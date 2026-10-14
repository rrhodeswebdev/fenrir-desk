"""`POST /api/settings/restart` on the wire (#262): a real `uvicorn` in a child process, so the
real SIGTERM reaches a process that is not pytest. The 202 arrives before the server goes, and
the lifespan's stop() runs: the graceful shutdown compose's restart policy follows."""

import os
import signal
import socket
import subprocess
import sys
import textwrap

import pytest

from fenrir.server.auth import SESSION_COOKIE

pytestmark = pytest.mark.socket

KEY = "k" * 43
SERVER = textwrap.dedent(f"""
    import socket, sys
    from pathlib import Path
    import uvicorn
    from fenrir.server.app import create_app
    from fenrir.settings import Settings

    tmp = Path(sys.argv[1])
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(rules_dir=tmp / "rules", data_dir=tmp / "data", private_dir=tmp / "private")
    app = create_app(settings, {KEY!r}, "c" * 22, None, port=port)
    stop = app.state.live.stop

    async def stopped():
        await stop()
        print("stopped", flush=True)

    app.state.live.stop = stopped
    print(port, flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
""")


def test_restart_answers_202_then_shuts_down_gracefully(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FENRIR_")}
    env |= {"FENRIR_BIND_ALL": "1", "FENRIR_ENV_FILE": str(tmp_path / ".env")}
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER, str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline())
        raw = (
            f"POST /api/settings/restart HTTP/1.1\r\nHost: localhost:{port}\r\n"
            f"Cookie: {SESSION_COOKIE}={KEY}\r\nOrigin: http://localhost:{port}\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(raw.encode())
            head = s.recv(4096).decode("latin-1")
        assert head.startswith("HTTP/1.1 202 ")
        # uvicorn re-raises the SIGTERM it shut down for, once the lifespan has finished
        assert proc.wait(timeout=20) == -signal.SIGTERM
        assert proc.stdout.read().split() == ["stopped"]
    finally:
        proc.kill()
        proc.wait()
