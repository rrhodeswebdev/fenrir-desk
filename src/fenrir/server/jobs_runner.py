"""Shell for backtest jobs: `data/jobs/<id>.json`, one runner thread, one child process per job.

Strategy code never runs in the server (DEC-1): each job's unfinished items run in one child
(`fenrir.server.job_child`, so the bars load once per job) with no `FENRIR_*` secrets in its
environment, as `FENRIR_PTY_USER` when set. The child exits after the job, so its spawn pool
and memoized bars go with it; the server never needs `fenrir.backtest.run.release()`.
"""

import contextlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Container, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fenrir.backtest.grid import GridItem
from fenrir.server.jobs import (
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

CHILD = (sys.executable, "-m", "fenrir.server.job_child")
KEY = re.compile(r"[0-9a-f]{64}")  # report_key's sha256 hex; it becomes a file name later

Listener = Callable[[dict[str, Any]], None]


def parse_event(line: str, indexes: Container[int]) -> tuple[int, dict[str, Any]]:
    """One child stdout line -> (item index, `JobItem` changes); ValueError for anything else.

    The child runs strategy code, so its output is untrusted: only these four shapes pass.
    """
    match json.loads(line):  # JSONDecodeError is a ValueError
        case {"i": int(i), "status": "running", **rest} if not rest:
            changes: dict[str, Any] = {"status": "running"}
        case {"i": int(i), "progress": [int(done), int(total)], **rest} if not rest:
            changes = {"progress": (done, total)}
        case {"i": int(i), "status": "done", "key": str(key), **rest} if not rest and KEY.fullmatch(
            key
        ):
            changes = {"status": "done", "key": key}
        case {"i": int(i), "status": "error", "error": str(error), **rest} if not rest:
            changes = {"status": "error", "error": error}
        case _:
            raise ValueError(f"not a job event: {line!r}")
    if i not in indexes:
        raise ValueError(f"not an item of this child: {line!r}")
    return i, changes


def strategy_user(env: Mapping[str, str]) -> str | None:
    """The user strategy code runs as: `FENRIR_PTY_USER`, or None for the server's own user.

    In the image (`FENRIR_BIND_ALL=1`) raises ValueError unless the server is root and that
    user is not (DEC-1).
    """
    user = env.get("FENRIR_PTY_USER") or None
    if env.get("FENRIR_BIND_ALL") == "1":
        # shortcut: the private_dir part of the check waits for Settings.private_dir (11.5/13)
        import pwd  # POSIX only, and only the Linux image sets FENRIR_BIND_ALL

        try:
            uid = pwd.getpwnam(user).pw_uid if user else 0
        except KeyError:
            uid = 0
        if uid == 0 or os.geteuid() != 0:
            raise ValueError(
                "refusing to run strategy code: the server must run as root with "
                f"FENRIR_PTY_USER naming a non-root user (got {user!r})"
            )
    return user


def child_env(env: Mapping[str, str], settings: Settings) -> dict[str, str]:
    """`env` without any `FENRIR_*` var (secrets), plus the settings the child needs."""
    return {k: v for k, v in env.items() if not k.startswith("FENRIR_")} | {
        "FENRIR_DATA_DIR": str(settings.data_dir),
        "FENRIR_RULES_DIR": str(settings.rules_dir),
        "FENRIR_STRATEGIES_DIR": str(settings.strategies_dir),
        "FENRIR_HOLDOUT_MONTHS": str(settings.holdout_months),
    }


def _kill_group(pid: int) -> None:
    # the child leads its own session, so this reaches its spawn workers too
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


class JobRunner:
    def __init__(self, settings: Settings, *, child: Sequence[str] = CHILD) -> None:
        self._settings = settings
        self._child = tuple(child)
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
        for path in sorted(self._dir.glob("*.json")):
            try:
                job = restart(from_json(json.loads(path.read_text())))
                if job.id != path.stem:  # the id names the file we write back to
                    raise ValueError(f"id {job.id!r} does not match the file name")
            except (ValueError, TypeError, KeyError, OSError) as e:
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

    def _write(self, job: Job) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(to_json(job), f)
                f.flush()
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
        try:
            proc = self._spawn(job, todo)
        except Exception as e:  # isolation refusal, or the child could not start
            if self._stopping:
                return  # left pending: the next start runs them
            for i in todo:
                self._update(job_id, i, {"status": "error", "error": f"{type(e).__name__}: {e}"})
            return
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    self._update(job_id, *parse_event(line, todo))
                except ValueError:
                    log.warning("backtest job %s: ignored child output %r", job_id, line[:200])
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
        left = set(unfinished(self.get(job_id))) & set(todo)
        for i in sorted(left):
            error = f"backtest process exited with code {code}"
            self._update(job_id, i, {"status": "error", "error": error})

    def _spawn(self, job: Job, todo: Sequence[int]) -> subprocess.Popen:
        user = strategy_user(os.environ)
        as_user: dict[str, Any] = {}
        if user:
            import pwd  # POSIX only; unset natively on Windows

            as_user = {"user": user, "group": pwd.getpwnam(user).pw_gid, "extra_groups": []}
        with self._lock:
            if self._stopping:
                raise RuntimeError("the job runner is stopping")
            # own session: stop() kills the child's whole group, its spawn workers included
            proc = subprocess.Popen(
                self._child,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=child_env(os.environ, self._settings),
                start_new_session=True,
                **as_user,
            )
            self._proc = proc
        items = [
            (i, job.items[i].strategy, job.items[i].account, job.items[i].contract) for i in todo
        ]
        with contextlib.suppress(BrokenPipeError):  # a child that died early is seen at wait()
            proc.stdin.write(json.dumps({"include_holdout": job.include_holdout, "items": items}))
            proc.stdin.close()
        return proc
