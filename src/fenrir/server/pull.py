"""Runs `fenrir data pull` as a child process, one at a time, for the Data page."""

import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fenrir.server.isolation import isolation_problems
from fenrir.settings import Settings

TAIL_LINES = 20
LINE_CHARS = 500


def pull_status(
    started_at: str | None, finished_at: str | None, returncode: int | None, log_tail: list[str]
) -> dict[str, Any]:
    if started_at is None:
        state = "idle"
    elif returncode is None:
        state = "running"
    else:
        state = "done" if returncode == 0 else "error"
    return {
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "returncode": returncode,
        "log_tail": log_tail,
    }


def pull_command() -> list[str]:
    # the server's own interpreter: the same install under `uv run` and in the image.
    # -P keeps the cwd (the terminal-writable repo) off sys.path: repo files cannot shadow fenrir.
    return [sys.executable, "-P", "-c", "from fenrir.cli.main import app; app()", "data", "pull"]


def pull_env(environ: Mapping[str, str], settings: Settings) -> dict[str, str]:
    # Not strategy code, so DEC-1's no-secrets rule for strategy children does not apply; it still
    # gets only the secret it needs. In Docker the key comes from the secrets file, not the env.
    # No FENRIR_ENV_FILE: its settings come from the env alone, so the NT token never reaches it.
    drop = ("FENRIR_NT_TOKEN", "FENRIR_ENV_FILE")
    env = {k: v for k, v in environ.items() if k not in drop}
    key = {"FENRIR_LSE_API_KEY": settings.lse_api_key} if settings.lse_api_key else {}
    return (
        env
        | key
        | {"FENRIR_DATA_DIR": str(settings.data_dir)}
        | {"PYTHONUNBUFFERED": "1"}  # unbuffered: log lines arrive as they are printed
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class PullRunner:
    """start() and status() never wait on the child, so both are safe on the event loop."""

    # shortcut: state lives in memory, so after a server restart an orphaned pull keeps running
    # and a new one can overlap it (same Parquet files); persist the pid or a lock if that bites
    def __init__(
        self,
        cmd: Sequence[str],
        env: Mapping[str, str] | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        """With `settings`, every start first passes the DEC-1 isolation check."""
        self._cmd = list(cmd)
        self._env = env
        self._settings = settings
        self._lock = threading.Lock()
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._returncode: int | None = None
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)

    def start(self) -> bool:
        """False while a pull runs; ValueError (nothing started) when the image is not isolated."""
        with self._lock:
            if self._started_at is not None and self._returncode is None:
                return False
            # Runs as the server's user (root in the image): the terminal's user could read the
            # LSE key from a child of its own (/proc/<pid>/environ). The store's writes are
            # symlink-safe instead (#135). Fail closed in an image that is not isolated.
            if self._settings and (problems := isolation_problems(self._settings)):
                raise ValueError("refusing to pull: " + "; ".join(problems))
            proc = subprocess.Popen(
                self._cmd,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            self._started_at, self._finished_at, self._returncode = _now(), None, None
            self._tail.clear()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        return True

    def _watch(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            with self._lock:
                self._tail.append(line.rstrip("\n")[:LINE_CHARS])
        returncode = proc.wait()
        with self._lock:
            self._finished_at, self._returncode = _now(), returncode

    def status(self) -> dict[str, Any]:
        with self._lock:
            return pull_status(
                self._started_at, self._finished_at, self._returncode, list(self._tail)
            )
