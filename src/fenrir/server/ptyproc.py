"""The shell behind `/ws/terminal`: one process in a pseudo-terminal. Shell (I/O).

POSIX: `pty.openpty` and a child in its own session with the PTY as its controlling tty; the
event loop watches the master fd, so nothing blocks it. Windows: pywinpty, read on a thread.
Both start in the server's cwd (the repo) and expose `read`, `write`, `resize`, `close`.
"""

import asyncio
import contextlib
import os
import signal
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

if sys.platform != "win32":
    import fcntl
    import pty
    import termios

COLS, ROWS = 80, 24  # until the client's first resize
CHUNK = 64 * 1024
HANGUP_GRACE = 1.0  # seconds for the shell to pass SIGHUP on to its jobs


async def _ready(add: Callable, remove: Callable, fd: int) -> None:
    fut = asyncio.get_running_loop().create_future()
    add(fd, lambda: fut.done() or fut.set_result(None))
    try:
        await fut
    finally:
        remove(fd)


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PosixPty:
    def __init__(self, argv: Sequence[str], env: Mapping[str, str], as_user: dict[str, Any]):
        master, slave = pty.openpty()
        try:
            _set_size(slave, COLS, ROWS)
            if as_user:  # so the PTY user can reopen /dev/pts/N (tty, ssh, gpg pinentry)
                os.fchown(slave, as_user["user"], as_user["group"])
            self._proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
                # runs after setsid and the privilege drop: take the PTY as controlling tty
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
                **as_user,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._fd = master

    async def read(self) -> bytes:
        """The next output; b"" once the shell side is gone."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                return os.read(self._fd, CHUNK)
            except BlockingIOError:
                await _ready(loop.add_reader, loop.remove_reader, self._fd)
            except OSError:  # EIO: every slave fd is closed
                return b""

    async def write(self, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self._fd, view) :]
            except BlockingIOError:  # the shell is not reading; wait rather than drop input
                await _ready(loop.add_writer, loop.remove_writer, self._fd)

    def resize(self, cols: int, rows: int) -> None:
        _set_size(self._fd, cols, rows)

    async def close(self) -> None:
        """Hang up, let the shell SIGHUP its jobs, then kill its whole process group."""
        os.close(self._fd)
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(self._proc.wait, HANGUP_GRACE)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)
        await asyncio.to_thread(self._proc.wait)


class WindowsPty:
    def __init__(self, argv: Sequence[str], env: Mapping[str, str], as_user: dict[str, Any]):
        from winpty import PtyProcess

        self._proc = PtyProcess.spawn(list(argv), env=dict(env), dimensions=(ROWS, COLS))

    async def read(self) -> bytes:
        try:
            return (await asyncio.to_thread(self._proc.read, CHUNK)).encode()
        except EOFError:
            return b""

    async def write(self, data: bytes) -> None:
        self._proc.write(data.decode())

    def resize(self, cols: int, rows: int) -> None:
        self._proc.setwinsize(rows, cols)

    async def close(self) -> None:
        self._proc.terminate(force=True)


def spawn(
    argv: Sequence[str], env: Mapping[str, str], as_user: dict[str, Any]
) -> PosixPty | WindowsPty:
    """`argv` in a PTY; OSError when it cannot start (e.g. no such shell), SubprocessError
    when taking the PTY as controlling tty fails. `as_user` holds
    `isolation.drop_to`'s Popen kwargs (POSIX only)."""
    return (WindowsPty if sys.platform == "win32" else PosixPty)(argv, env, as_user)
