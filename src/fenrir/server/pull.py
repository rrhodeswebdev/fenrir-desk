"""Runs `fenrir data pull` as a child process, one at a time, for the Data page."""

import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fenrir.server.isolation import as_user, isolation_problems
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
    # No FENRIR_ENV_FILE: the child may run unprivileged, so its settings come from the env alone.
    drop = ("FENRIR_NT_TOKEN", "FENRIR_ENV_FILE")
    env = {k: v for k, v in environ.items() if k not in drop}
    key = {"FENRIR_LSE_API_KEY": settings.lse_api_key} if settings.lse_api_key else {}
    return (
        env
        | key
        | {"FENRIR_DATA_DIR": str(settings.data_dir)}
        | {"PYTHONUNBUFFERED": "1"}  # unbuffered: log lines arrive as they are printed
    )


def pull_user(settings: Settings) -> str | None:
    """`FENRIR_PTY_USER` in the image (`FENRIR_BIND_ALL=1`), else None: natively the pull runs as
    the server's user. ValueError with the isolation reasons when the image is not isolated."""
    if problems := isolation_problems(settings):
        raise ValueError("refusing to pull: " + "; ".join(problems))
    # isolation_problems is empty in the image only when FENRIR_PTY_USER names a user
    return os.environ["FENRIR_PTY_USER"] if os.environ.get("FENRIR_BIND_ALL") == "1" else None


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
        """With `settings`, every start runs the child as `pull_user(settings)`."""
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
            # data/ is terminal-writable in the image: never pull into it as root (DEC-1, #135)
            overlay, drop = as_user(pull_user(self._settings) if self._settings else None)
            proc = subprocess.Popen(
                self._cmd,
                env={**(os.environ if self._env is None else self._env), **overlay},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                **drop,
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
