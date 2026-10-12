"""`WS /ws/terminal` end to end: a real uvicorn server and a real PTY running `/bin/sh`."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from fenrir.server import ptyproc
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.routes import terminal
from fenrir.settings import Settings

pytestmark = [
    pytest.mark.socket,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY"),
]

KEY = "k" * 43


@pytest.fixture(scope="module")
def port(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("serve")
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(rules_dir=tmp / "rules", private_dir=tmp / "private")
    app = create_app(settings, KEY, "c" * 22, None, port=port)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    yield port
    server.should_exit = True
    thread.join(10)
    sock.close()


@pytest.fixture(autouse=True)
def native_sh(monkeypatch):
    for var in ("FENRIR_BIND_ALL", "FENRIR_PTY_USER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SHELL", "/bin/sh")


def open_term(port, cookie=KEY, origin=None):
    return connect(
        f"ws://localhost:{port}/ws/terminal",
        sock=socket.create_connection(("127.0.0.1", port), timeout=5),
        origin=origin or f"http://localhost:{port}",
        additional_headers={"Cookie": f"{SESSION_COOKIE}={cookie}"},
        open_timeout=5,
    )


def send(ws, data):
    ws.send(json.dumps({"type": "input", "data": data}))


def read_until(ws, pattern: bytes) -> bytes:
    out = b""
    while not re.search(pattern, out):
        out += ws.recv(timeout=5)
    return out


def gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_runs_input_and_streams_output(port):
    with open_term(port) as ws:
        ws.send(b"\x00binary is ignored")
        ws.send("not json")
        send(ws, "echo $((40+2)) a\ud800b\n")  # a lone surrogate is replaced, not fatal
        out = read_until(ws, rb"42 a\?b")
        assert isinstance(out, bytes)


def test_resize_reaches_the_pty(port):
    with open_term(port) as ws:
        ws.send(json.dumps({"type": "resize", "cols": 123, "rows": 45}))
        send(ws, "stty size\n")
        read_until(ws, rb"45 123")


def test_exit_closes_the_socket(port):
    with open_term(port) as ws:
        send(ws, "exit\n")
        with pytest.raises(ConnectionClosed) as e:
            while True:
                ws.recv(timeout=5)
        assert e.value.rcvd.code == 1000


@pytest.fixture(params=["bash", "zsh", "dash"])
def job_shell(request, monkeypatch):
    # job control puts each job in its own process group; only bash passes SIGHUP on to them
    path = shutil.which(request.param) or pytest.skip(f"{request.param} is not installed")
    monkeypatch.setenv("SHELL", path)


def test_disconnect_kills_the_shell_and_its_jobs(port, job_shell):
    with open_term(port) as ws:
        send(ws, "sleep 1000 & echo job=$!\n")  # `$!` ends each line: bash's history expansion
        send(ws, "nohup sleep 1000 >/dev/null 2>&1 & echo pids=$$,$!\n")
        out = read_until(ws, rb"pids=\d+,\d+")
        pids = map(int, re.search(rb"job=(\d+).*pids=(\d+),(\d+)", out, re.S).groups())
    assert all(map(gone, pids))


def test_disconnect_kills_jobs_forked_during_the_kill(port, job_shell):
    with open_term(port) as ws:
        send(ws, "(while :; do sleep 1001 & sleep 0.005; done) & echo pid=$$\n")
        shell = int(re.search(rb"pid=(\d+)", read_until(ws, rb"pid=\d+\r?\n")).group(1))
        time.sleep(0.2)  # let it fork a few
    deadline = time.monotonic() + 5  # a survivor of the sweep sleeps for 1001 s
    while (survivors := ptyproc._session_pids(shell)) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in survivors:  # don't leak them if this fails
        os.kill(pid, signal.SIGKILL)
    assert survivors == []


def test_disconnect_with_unread_input_kills_the_shell(port):
    with open_term(port) as ws:
        send(ws, "echo pid=$$; exec sleep 1000\n")  # the shell becomes a process that never reads
        shell = int(re.search(rb"pid=(\d+)", read_until(ws, rb"pid=\d+\r?\n")).group(1))
        for _ in range(1024):  # far more than the tty input buffer holds
            send(ws, "x" * 63 + "\n")
    assert gone(shell)


def test_too_much_unread_input_closes_1009(port):
    with open_term(port) as ws:
        send(ws, "exec sleep 1000\n")
        with pytest.raises(ConnectionClosed) as e:
            for _ in range(terminal.MAX_PENDING // 65536 + 4):  # small frames queue behind
                send(ws, ("x" * 63 + "\n") * 1024)  # whole lines: the tty blocks, not drops
                # stop at the close: unread frames would reset the TCP and lose its code
                with contextlib.suppress(TimeoutError):
                    while True:
                        ws.recv(timeout=0.1)
        assert e.value.rcvd.code == 1009


def test_shell_env_has_no_secrets(port, monkeypatch):
    monkeypatch.setenv("FENRIR_NT_TOKEN", "nt-secret")
    monkeypatch.setenv("LEAKY", f"x{KEY}x")
    with open_term(port) as ws:
        send(ws, "env; echo END$((1+1))\n")
        out = read_until(ws, rb"END2")
    assert b"FENRIR_" not in out and b"nt-secret" not in out and KEY.encode() not in out
    assert b"TERM=xterm-256color" in out


def test_pty_user_overlay_and_shell(port, monkeypatch):
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    monkeypatch.setenv("SHELL", "/no/such/shell")  # the PTY user's own shell wins
    overlay = {"HOME": "/home/claude", "USER": "claude", "LOGNAME": "claude", "SHELL": "/bin/sh"}
    # privileges cannot drop in a non-root test, so only the env overlay is real here
    monkeypatch.setattr(terminal, "drop_to", lambda user: (overlay, {}))
    with open_term(port) as ws:
        send(ws, 'echo "h=$HOME u=$USER"\n')
        read_until(ws, rb"h=/home/claude u=claude")


def test_unknown_pty_user_is_refused(port, monkeypatch, caplog):
    monkeypatch.setenv("FENRIR_PTY_USER", "no-such-user-fenrir")
    with pytest.raises(InvalidStatus) as e:
        open_term(port)
    assert e.value.response.status_code == 403
    assert "no-such-user-fenrir" in caplog.text


@pytest.mark.parametrize("cookie, origin", [("wrong", None), (KEY, "http://evil.example")])
def test_bad_cookie_or_origin_is_refused(port, cookie, origin):
    with pytest.raises(InvalidStatus) as e:
        open_term(port, cookie, origin)
    assert e.value.response.status_code == 403


def test_image_refuses_when_not_isolated(port, monkeypatch, caplog):
    monkeypatch.setattr(terminal, "isolation_problems", lambda settings: ["server is not root"])
    with pytest.raises(InvalidStatus) as e:
        open_term(port)
    assert e.value.response.status_code == 403
    assert "server is not root" in caplog.text


def test_missing_shell_closes_with_a_message(port, monkeypatch):
    monkeypatch.setenv("SHELL", "/no/such/shell")
    with open_term(port) as ws:
        assert b"cannot start the shell /no/such/shell" in ws.recv(timeout=5)
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
        assert e.value.rcvd.code == 1011


def test_failed_controlling_tty_closes_with_a_message(port, monkeypatch):
    def fail(argv, env, as_user):
        raise subprocess.SubprocessError("Exception occurred in preexec_fn.")

    monkeypatch.setattr(ptyproc, "spawn", fail)
    with open_term(port) as ws:
        assert b"cannot start the shell /bin/sh: Exception occurred in preexec_fn." in ws.recv(
            timeout=5
        )
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
        assert e.value.rcvd.code == 1011


def test_unexpected_task_error_is_logged(port, monkeypatch, caplog):
    spawn = ptyproc.spawn

    def broken_resize(argv, env, as_user):
        pty = spawn(argv, env, as_user)
        pty.resize = lambda cols, rows: (_ for _ in ()).throw(OSError("resize broke"))
        return pty

    monkeypatch.setattr(ptyproc, "spawn", broken_resize)
    with open_term(port) as ws:
        ws.send(json.dumps({"type": "resize", "cols": 10, "rows": 10}))
        with pytest.raises(ConnectionClosed):
            while True:
                ws.recv(timeout=5)
    assert "terminal session failed" in caplog.text and "resize broke" in caplog.text


def test_cancelled_close_still_kills_the_session():
    async def cancel_close() -> int:
        # shell and job both ignore the hang-up, so only close()'s kills can end them
        script = "trap '' HUP; set -m; sleep 1000 & echo job=$!; exec sleep 1000"
        argv = ["/bin/sh", "-c", script]
        pty = ptyproc.spawn(argv, {"PATH": os.environ["PATH"]}, {})
        out = b""
        while not re.search(rb"job=\d+\r?\n", out):
            out += await pty.read()
        task = asyncio.create_task(pty.close())
        await asyncio.sleep(0.1)  # inside the hang-up grace, before any kill
        task.cancel()
        return int(re.search(rb"job=(\d+)", out).group(1))

    assert gone(asyncio.run(cancel_close()))


def test_session_scan_without_ps_is_empty_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(ptyproc.os.path, "isdir", lambda path: False)  # the macOS branch

    def no_ps(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "/bin/ps")

    monkeypatch.setattr(ptyproc.subprocess, "run", no_ps)
    assert ptyproc._session_pids(1) == []
    assert "cannot list the shell's session" in caplog.text
