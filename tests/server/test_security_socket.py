"""SPEC SC7 on the wire: a real uvicorn server on a loopback port, raw requests against it.

TestClient cannot show what uvicorn sends for a refused WebSocket, so this reads the status line.
"""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi import WebSocket
from websockets.sync.client import connect

from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE, check_ws
from fenrir.settings import Settings

pytestmark = pytest.mark.socket

KEY = "k" * 43
CODE = "c" * 22
EVIL = "http://evil.example"
WS_HEADERS = (
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
)


@pytest.fixture(scope="module")
def port(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("serve")
    sock = socket.create_server(("127.0.0.1", 0))  # port 0: no race for a free port
    port = sock.getsockname()[1]
    settings = Settings(rules_dir=tmp / "rules", private_dir=tmp / "private")
    app = create_app(settings, KEY, CODE, None, port=port)

    @app.websocket("/ws/probe")  # test-only route; no static_dir, so no SPA catch-all to precede
    async def probe(ws: WebSocket) -> None:
        if not check_ws(ws):
            await ws.close()
            return
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

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
    assert not thread.is_alive(), "uvicorn did not stop"


def request(port: int, line: str, host: str = "localhost", headers: str = "") -> str:
    """Send one raw request; return the response head (status line and headers)."""
    host_line = f"Host: {host}:{port}\r\n" if host else ""
    raw = f"{line} HTTP/1.1\r\n{host_line}{headers}\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(raw.encode())
        head = b""
        while b"\r\n\r\n" not in head and (chunk := s.recv(4096)):
            head += chunk
    return head.split(b"\r\n\r\n")[0].decode("latin-1")


def status(head: str) -> int:
    return int(head.split(" ", 2)[1])


def cookie(value: str = KEY) -> str:
    return f"Cookie: {SESSION_COOKIE}={value}\r\n"


def origin(port: int) -> str:
    return f"Origin: http://localhost:{port}\r\n"


def test_api_needs_the_session_cookie(port):
    assert status(request(port, "GET /api/rules")) == 401
    assert status(request(port, "GET /api/rules", headers=cookie("wrong"))) == 401
    assert status(request(port, "GET /api/rules", headers=cookie())) == 200
    assert status(request(port, "GET /api/health")) == 200


def test_foreign_or_repeated_host_400(port):
    for path in ("/api/health", "/api/rules", f"/launch?code={CODE}"):
        assert status(request(port, f"GET {path}", host="evil.example")) == 400
    assert status(request(port, "GET /ws/probe", host="evil.example", headers=WS_HEADERS)) == 400
    twice = f"Host: localhost:{port}\r\n"
    assert status(request(port, "GET /api/health", headers=twice)) == 400
    assert status(request(port, "GET /api/health", host="")) == 400


def test_127_redirects_to_localhost(port):
    head = request(port, "GET /api/health?x=1", host="127.0.0.1")
    assert status(head) == 307
    assert f"location: http://localhost:{port}/api/health?x=1" in head.lower()


def test_launch_code_is_single_use(port):
    assert status(request(port, "GET /launch?code=wrong")) == 403
    head = request(port, f"GET /launch?code={CODE}")
    assert status(head) == 303
    assert f"set-cookie: {SESSION_COOKIE}={KEY};" in head.lower()
    assert status(request(port, f"GET /launch?code={CODE}")) == 403


def test_cross_origin_write_403(port):
    put = "PUT /api/rules/none"
    assert status(request(port, put, headers=cookie() + f"Origin: {EVIL}\r\n")) == 403
    assert status(request(port, put, headers=cookie())) == 403
    # the same write from our own origin passes the guard and reaches the route
    assert status(request(port, put, headers=cookie() + origin(port))) == 404  # no such account


def test_refused_websocket_handshake_is_http_403(port):
    ws = "GET /ws/probe"
    assert status(request(port, ws, headers=WS_HEADERS + origin(port))) == 403
    assert status(request(port, ws, headers=WS_HEADERS + cookie("wrong") + origin(port))) == 403
    assert status(request(port, ws, headers=WS_HEADERS + cookie() + f"Origin: {EVIL}\r\n")) == 403
    assert status(request(port, ws, headers=WS_HEADERS + cookie())) == 403


def test_good_websocket_handshake_is_accepted(port):
    # connect() raises unless the handshake answers 101
    # the server listens on 127.0.0.1 only; connect there so nothing on ::1 can answer
    with connect(
        f"ws://localhost:{port}/ws/probe",
        sock=socket.create_connection(("127.0.0.1", port), timeout=5),
        origin=f"http://localhost:{port}",
        additional_headers={"Cookie": f"{SESSION_COOKIE}={KEY}"},
        open_timeout=5,
    ) as ws:
        assert ws.recv(timeout=5) == "hi"
