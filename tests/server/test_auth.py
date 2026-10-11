import secrets

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fenrir.server.auth import (
    SESSION_COOKIE,
    allowed_origins,
    check_ws,
    csrf_ok,
    origin_ok,
    require_session,
    session_ok,
)

KEY = "k" * 43
ORIGIN = "http://localhost:8000"
ALLOWED = allowed_origins(8000, dev=False)


def test_session_ok_matches():
    assert session_ok(KEY, KEY)


def test_session_ok_rejects_wrong_missing_and_empty():
    assert not session_ok(KEY, KEY[:-1] + "x")
    assert not session_ok(KEY, None)
    assert not session_ok(KEY, "")
    assert not session_ok("", "")
    assert not session_ok("", None)


def test_session_ok_rejects_non_ascii_without_raising():
    assert not session_ok(KEY, "é" * 43)


def test_session_ok_uses_compare_digest(monkeypatch):
    calls = []
    real = secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(secrets, "compare_digest", spy)
    assert session_ok(KEY, KEY)
    assert len(calls) == 1


def test_allowed_origins_prod():
    assert ALLOWED == frozenset({"http://127.0.0.1:8000", "http://localhost:8000"})


def test_allowed_origins_dev_adds_vite():
    assert allowed_origins(8000, dev=True) == ALLOWED | {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }


def test_origin_ok():
    assert origin_ok("http://localhost:8000", ALLOWED)
    assert origin_ok("http://127.0.0.1:8000", ALLOWED)
    assert not origin_ok("http://evil.example:8000", ALLOWED)
    assert not origin_ok("http://localhost:8001", ALLOWED)
    assert not origin_ok("http://localhost:5173", ALLOWED)
    assert not origin_ok("null", ALLOWED)
    assert not origin_ok(None, ALLOWED)


def test_csrf_get_always_passes():
    assert csrf_ok("GET", None, None, ALLOWED)
    assert csrf_ok("GET", "http://evil.example", "cross-site", ALLOWED)


def test_csrf_non_get_needs_own_origin():
    assert csrf_ok("POST", ORIGIN, None, ALLOWED)
    assert csrf_ok("POST", ORIGIN, "same-origin", ALLOWED)
    assert not csrf_ok("POST", None, None, ALLOWED)
    assert not csrf_ok("PUT", "http://evil.example", None, ALLOWED)
    assert not csrf_ok("DELETE", "http://localhost:5173", "same-origin", ALLOWED)


def test_csrf_non_get_refuses_cross_site_fetch():
    assert not csrf_ok("POST", ORIGIN, "cross-site", ALLOWED)
    assert not csrf_ok("POST", ORIGIN, "same-site", ALLOWED)
    assert not csrf_ok("POST", ORIGIN, "none", ALLOWED)


def _app() -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    app.state.session_key = KEY
    app.state.allowed_origins = ALLOWED
    accepted: list[str] = []
    router = APIRouter(prefix="/api/x", dependencies=[Depends(require_session)])

    @router.get("")
    def read() -> dict:
        return {"ok": True}

    @router.post("")
    def write() -> dict:
        return {"ok": True}

    app.include_router(router)

    @app.websocket("/ws/x")
    async def ws_x(ws: WebSocket) -> None:
        if not check_ws(ws):
            await ws.close()
            return
        await ws.accept()
        accepted.append("yes")
        await ws.send_text("hi")
        await ws.close()

    return app, accepted


def _cookie(value: str = KEY) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={value}"}


def test_require_session_401_without_or_with_bad_cookie():
    client = TestClient(_app()[0])
    assert client.get("/api/x").status_code == 401
    assert client.get("/api/x", headers=_cookie("wrong")).status_code == 401


def test_require_session_200_with_cookie():
    client = TestClient(_app()[0])
    assert client.get("/api/x", headers=_cookie()).json() == {"ok": True}
    assert client.post("/api/x", headers=_cookie() | {"origin": ORIGIN}).status_code == 200


def test_require_session_403_for_cross_origin_write():
    client = TestClient(_app()[0])
    assert client.post("/api/x", headers=_cookie()).status_code == 403
    foreign = _cookie() | {"origin": "http://evil.example"}
    assert client.post("/api/x", headers=foreign).status_code == 403
    cross = _cookie() | {"origin": ORIGIN, "sec-fetch-site": "cross-site"}
    assert client.post("/api/x", headers=cross).status_code == 403


def test_check_ws_accepts_good_cookie_and_origin():
    app, accepted = _app()
    with TestClient(app).websocket_connect("/ws/x", headers=_cookie() | {"origin": ORIGIN}) as ws:
        assert ws.receive_text() == "hi"
    assert accepted == ["yes"]


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": ORIGIN},
        _cookie("wrong") | {"origin": ORIGIN},
        _cookie() | {"origin": "http://evil.example"},
        _cookie(),
    ],
    ids=["no-cookie", "bad-cookie", "foreign-origin", "no-origin"],
)
def test_check_ws_refuses_before_accept(headers):
    app, accepted = _app()
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws/x", headers=headers):
            pass
    assert accepted == []
