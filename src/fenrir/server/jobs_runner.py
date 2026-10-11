"""Shell for backtest jobs: `data/jobs/<id>.json`, one runner thread, one child process per job.

Strategy code never runs in the server (DEC-1): grid expansion (`expand`, which loads each
strategy to read its MARKET) and each job's unfinished items run in `fenrir.server.job_child`
(one child per job, so the bars load once) with no `FENRIR_*` secrets in its environment, as
`FENRIR_PTY_USER` when set. The child exits after the job, so its spawn pool and memoized bars
go with it; the server never needs `fenrir.backtest.run.release()`.
"""

import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Container, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fenrir.backtest.grid import GridItem
from fenrir.core.instruments import load_instruments
from fenrir.rules.loader import list_accounts
from fenrir.server import terminal
from fenrir.server.isolation import as_user, isolation_problems
from fenrir.server.jobs import (
    KEY,
    Job,
    from_json,
    new_job,
    progress_step,
    restart,
    to_json,
    unfinished,
    update_item,
)
from fenrir.settings import Settings

log = logging.getLogger(__name__)

# -P: no cwd on sys.path, so a planted fenrir/ or polars.py in the repo (writable by the
# terminal) is never imported in place of the installed module
CHILD = (sys.executable, "-P", "-m", "fenrir.server.job_child")
LINE_MAX = 64 * 1024  # longer child lines are dropped whole
ERROR_MAX = 2048

Listener = Callable[[dict[str, Any]], None]


def _json(line: str) -> Any:
    try:
        return json.loads(line)  # JSONDecodeError is a ValueError
    except RecursionError:
        raise ValueError("JSON nested too deep") from None


def parse_event(line: str, indexes: Container[int]) -> tuple[int, dict[str, Any]]:
    """One child stdout line -> (item index, `JobItem` changes); ValueError for anything else.

    The child runs strategy code, so its output is untrusted: only these four shapes pass.
    """
    match _json(line):
        case {"i": int(i), "status": "running", **rest} if not rest:
            changes: dict[str, Any] = {"status": "running"}
        case {"i": int(i), "progress": [int(done), int(total)], **rest} if not rest:
            changes = {"progress": (done, total)}
        case {"i": int(i), "status": "done", "key": str(key), **rest} if not rest and KEY.fullmatch(
            key
        ):
            changes = {"status": "done", "key": key}
        case {"i": int(i), "status": "error", "error": str(error), **rest} if not rest:
            changes = {"status": "error", "error": error[:ERROR_MAX]}
        case _:
            raise ValueError(f"not a job event: {line[:200]!r}")
    if i not in indexes:
        raise ValueError(f"not an item of this child: {line[:200]!r}")
    return i, changes


def parse_grid(line: str, settings: Settings) -> tuple[GridItem, ...]:
    """The `--expand` child's answer; ValueError with its error text, or for anything else."""
    match data := _json(line):
        case {"error": str(error)} if len(data) == 1:
            raise ValueError(error[:ERROR_MAX])
        case list(rows):
            pass
        case _:
            raise ValueError(f"not a grid: {line[:200]!r}")
    accounts = set(list_accounts(settings.rules_dir))
    contracts = {s.symbol for s in load_instruments().specs}
    root = settings.strategies_dir.resolve()
    grid = []
    for row in rows:
        match row:
            case [str(path), str(account), str(contract)] if (
                account in accounts
                and contract in contracts
                and Path(path).resolve().is_relative_to(root)
            ):
                grid.append(GridItem(Path(path), account, contract))
            case _:
                raise ValueError(f"not a grid row: {row!r}"[:300])
    return tuple(grid)


def strategy_user(settings: Settings) -> str | None:
    """The user strategy code runs as: `FENRIR_PTY_USER`, or None for the server's own user.

    In the image (`FENRIR_BIND_ALL=1`) raises ValueError with the reasons when the DEC-1
    isolation check fails.
    """
    if problems := isolation_problems(settings):
        raise ValueError("refusing to run strategy code: " + "; ".join(problems))
    return os.environ.get("FENRIR_PTY_USER") or None


def _kill_group(pid: int) -> None:
    # the child leads its own session, so this reaches its spawn workers too
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _read(proc: subprocess.Popen, idle: float) -> Iterator[str]:
    """`proc`'s whole stdout lines up to LINE_MAX. After `idle` seconds without one, kills its
    group and raises TimeoutError."""
    lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def pump() -> None:
        skipping = False  # inside an overlong line: its tail is not a line of its own
        while chunk := proc.stdout.readline(LINE_MAX):
            whole = chunk.endswith("\n")
            if whole and not skipping:
                lines.put(chunk)
            skipping = not whole
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=idle)
        except queue.Empty:
            _kill_group(proc.pid)
            raise TimeoutError(f"no progress for {idle / 60:g} min") from None
        if line is None:
            return
        yield line


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        child: Sequence[str] = CHILD,
        secret: str = "",
        idle: float = 30 * 60,
        expand_timeout: float = 60,
    ) -> None:
        """`secret` (the session key): no env var holding it reaches the child."""
        self._settings = settings
        self._child = tuple(child)
        self._secret = secret
        self._idle = idle
        self._expand_timeout = expand_timeout
        self._dir = settings.data_dir / "jobs"
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._jobs: dict[str, Job] = {}
        self._listeners: list[Listener] = []
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stopping = False

    def start(self) -> None:
        """Load `data/jobs/*.json`, queue unfinished items, start the runner thread. Idempotent."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._work, name="backtest-jobs", daemon=True)
        if link := self._symlinked():
            log.error("not loading backtest jobs: %s is a symlink", link)
        for path in [] if link else sorted(self._dir.glob("*.json")):
            try:
                job = restart(from_json(json.loads(path.read_text())))
                if job.id != path.stem:  # the id names the file we write back to
                    raise ValueError(f"id {job.id!r} does not match the file name")
            except (ValueError, TypeError, KeyError, OSError, RecursionError) as e:
                log.warning("skipping job file %s: %r", path, e)
                continue
            self._set(job)
            if unfinished(job):
                self._queue.put(job.id)
        self._thread.start()

    def stop(self) -> None:
        """Kill the running child and its workers; its unfinished items re-run on next start."""
        with self._lock:
            self._stopping = True
            if self._proc is not None:
                _kill_group(self._proc.pid)
        self._queue.put(None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)

    def expand(
        self, strategies: Sequence[str], accounts: Sequence[str], contracts: Sequence[str] | None
    ) -> tuple[GridItem, ...]:
        """`expand_grid` in an isolated child, since it loads every strategy file.

        ValueError with the child's error text, on a timeout, or for an answer outside
        `strategies_dir`, the known accounts or the known contracts.
        """
        request = {
            "strategies": list(strategies),
            "accounts": list(accounts),
            "contracts": None if contracts is None else list(contracts),
        }
        proc = self._spawn(["--expand"], request)
        try:
            lines = list(_read(proc, self._expand_timeout))
        except TimeoutError:
            raise ValueError(
                f"loading the strategies took over {self._expand_timeout:g} s"
            ) from None
        finally:
            code = proc.wait()
            _kill_group(proc.pid)
        if len(lines) != 1:
            raise ValueError(f"the strategy loader exited with code {code} without an answer")
        return parse_grid(lines[0], self._settings)

    def submit(self, grid: Sequence[GridItem], include_holdout: bool) -> Job:
        created = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        job = new_job(uuid.uuid4().hex, created, grid, include_holdout)
        self._set(job)
        self._queue.put(job.id)
        return job

    def jobs(self) -> list[Job]:
        """Newest first."""
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def add_listener(self, listener: Listener) -> None:
        """`listener(to_json(job))` on every change, called from the runner or submit thread."""
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.remove(listener)

    def _symlinked(self) -> Path | None:
        # shortcut: checked, not opened with O_NOFOLLOW; a swap between check and write still
        # races, upgrade to dir fds if the terminal is ever given a way to win that race
        return next((p for p in (self._settings.data_dir, self._dir) if p.is_symlink()), None)

    def _write(self, job: Job) -> None:
        if self._symlinked():
            return  # never write through a link the terminal made; _run logs it and errors items
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(to_json(job), f)
                f.flush()
                os.fchmod(f.fileno(), 0o644)  # the terminal may read data/jobs (SPEC)
                os.fsync(f.fileno())
            os.replace(tmp, self._dir / f"{job.id}.json")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _set(self, job: Job, *, persist: bool = True) -> None:
        with self._lock:
            if persist:  # written first, so a job shown done is also done on disk
                self._write(job)
            self._jobs[job.id] = job
            listeners = list(self._listeners)
        payload = to_json(job)
        for listener in listeners:
            try:
                listener(payload)
            except Exception:
                log.exception("job listener failed")

    def _update(self, job_id: str, index: int, changes: dict[str, Any]) -> None:
        job = self.get(job_id)
        assert job is not None  # only this thread changes a known job
        if "progress" in changes:  # not persisted: a restart resets progress anyway
            if progress_step(job.items[index].progress, changes["progress"]):
                self._set(update_item(job, index, **changes), persist=False)
        else:
            self._set(update_item(job, index, **changes))

    def _fail(self, job_id: str, indexes: Sequence[int], error: str) -> None:
        for i in indexes:
            self._update(job_id, i, {"status": "error", "error": error})

    def _work(self) -> None:
        while (job_id := self._queue.get()) is not None:
            try:
                self._run(job_id)
            except Exception:  # never let one job stop the queue
                log.exception("backtest job %s failed", job_id)

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        todo = unfinished(job) if job else ()
        if not todo:
            return
        if link := self._symlinked():
            log.error("refusing backtest job %s: %s is a symlink", job_id, link)
            return self._fail(job_id, todo, f"refusing to run: {link} is a symlink")
        items = [
            (i, job.items[i].strategy, job.items[i].account, job.items[i].contract) for i in todo
        ]
        try:
            proc = self._spawn(
                [], {"include_holdout": job.include_holdout, "items": items}, job=True
            )
        except Exception as e:  # isolation refusal, or the child could not start
            if self._stopping:
                return  # left pending: the next start runs them
            return self._fail(job_id, todo, f"{type(e).__name__}: {e}")
        error = None
        try:
            for line in _read(proc, self._idle):
                try:
                    event = parse_event(line, todo)
                except ValueError:
                    log.warning("backtest job %s: ignored child output %r", job_id, line[:200])
                    continue
                self._update(job_id, *event)
        except TimeoutError as e:
            error = str(e)
        except BaseException:
            _kill_group(proc.pid)  # e.g. a failed job write: never leave the child blocked
            raise
        finally:
            code = proc.wait()
            with self._lock:
                self._proc = None
                stopping = self._stopping
        _kill_group(proc.pid)  # workers orphaned by a child that crashed
        if stopping:
            return  # left as they are: the next start re-runs them
        left = sorted(set(unfinished(self.get(job_id))) & set(todo))
        self._fail(job_id, left, error or f"backtest process exited with code {code}")

    def _spawn(
        self, args: Sequence[str], request: object, *, job: bool = False
    ) -> subprocess.Popen:
        """A child in its own session (so its whole group can be killed), as the strategy user."""
        user = strategy_user(self._settings)
        env = terminal.child_env(os.environ, self._secret) | {
            "FENRIR_DATA_DIR": str(self._settings.data_dir),
            "FENRIR_RULES_DIR": str(self._settings.rules_dir),
            "FENRIR_STRATEGIES_DIR": str(self._settings.strategies_dir),
            "FENRIR_HOLDOUT_MONTHS": str(self._settings.holdout_months),
        }
        overlay, drop = as_user(user)
        env |= overlay
        with self._lock:
            if job and self._stopping:
                raise RuntimeError("the job runner is stopping")
            proc = subprocess.Popen(
                [*self._child, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=env,
                start_new_session=True,
                **drop,
            )
            if job:
                self._proc = proc
        with contextlib.suppress(BrokenPipeError):  # a child that died early is seen at wait()
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
        return proc
