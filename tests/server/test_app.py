import pytest
from fastapi import APIRouter, Depends, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from fenrir.server.app import create_app, host_action
from fenrir.server.auth import SESSION_COOKIE, check_ws, require_session
from fenrir.settings import Settings

KEY = "k" * 43
CODE = "c" * 22
BASE = "http://localhost:8000"
ORIGIN = {"origin": BASE}
WS = "ws://localhost:8000/ws/probe"


def _client(*, follow: bool = True) -> tuple[TestClient, list[str]]:
    app = create_app(Settings(), KEY, CODE, None, port=8000)
    accepted: list[str] = []
    router = APIRouter(prefix="/api/probe", dependencies=[Depends(require_session)])

    @router.get("")
    def read() -> dict:
        return {"ok": True}

    @router.post("")
    def write() -> dict:
        return {"ok": True}

    app.include_router(router)

    @app.websocket("/ws/probe")
    async def ws_probe(ws: WebSocket) -> None:
        if not check_ws(ws):
            await ws.close()
            return
        await ws.accept()
        accepted.append("yes")
        await ws.send_text("hi")
        await ws.close()

    return TestClient(app, base_url=BASE, follow_redirects=follow), accepted


def _cookie(value: str = KEY) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={value}"}


def test_create_app_sets_state():
    settings = Settings()
    app = create_app(settings, KEY, CODE, None, port=9000, dev=True)
    assert app.state.settings is settings
    assert app.state.session_key == KEY
    assert "http://localhost:9000" in app.state.allowed_origins
    assert "http://localhost:5173" in app.state.allowed_origins


def test_health_needs_no_cookie():
    r = _client()[0].get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_api_route_401_without_or_with_bad_cookie_200_with():
    client = _client()[0]
    assert client.get("/api/probe").status_code == 401
    assert client.get("/api/probe", headers=_cookie("wrong")).status_code == 401
    assert client.get("/api/probe", headers=_cookie()).json() == {"ok": True}
    assert client.post("/api/probe", headers=_cookie() | ORIGIN).status_code == 200


def test_cross_origin_post_403():
    client = _client()[0]
    assert client.post("/api/probe", headers=_cookie()).status_code == 403
    evil = _cookie() | {"origin": "http://evil.example"}
    assert client.post("/api/probe", headers=evil).status_code == 403


def test_unknown_api_path_404():
    assert _client()[0].get("/api/nope").status_code == 404


def test_no_dev_token_route():
    app = create_app(Settings(), KEY, CODE, None, port=8000, dev=True)
    assert TestClient(app, base_url=BASE).get("/api/dev-token").status_code == 404


@pytest.mark.parametrize(
    ("host", "action"),
    [
        ("localhost", "ok"),
        ("localhost:8000", "ok"),
        ("127.0.0.1", "redirect"),
        ("127.0.0.1:5173", "redirect"),
        ("evil.example", "reject"),
        ("evil.example:8000", "reject"),
        ("localhost.evil.example", "reject"),
        ("[::1]:8000", "reject"),
        ("127.0.0.1:@evil.example", "reject"),
        ("localhost:8000@evil.example", "reject"),
        ("", "reject"),
        (None, "reject"),
    ],
)
def test_host_action(host, action):
    assert host_action(host) == action


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/probe", "/launch?code=" + CODE])
def test_foreign_host_400_without_secrets(path):
    client = _client()[0]
    r = client.get(path, headers=_cookie() | {"host": "evil.example:8000"})
    assert r.status_code == 400
    assert KEY not in r.text and CODE not in r.text
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")


@pytest.mark.parametrize("path", ["/", "/api/health", "/launch?code=x&y=%2F", "/a/b?q=1"])
def test_loopback_ip_redirects_to_localhost(path):
    client = _client(follow=False)[0]
    r = client.get(path, headers={"host": "127.0.0.1:8000"})
    assert r.status_code == 307
    assert r.headers["location"] == "http://localhost:8000" + path


def test_launch_sets_cookie_once():
    client = _client(follow=False)[0]
    r = client.get(f"/launch?code={CODE}")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    attrs = {a.strip().lower() for a in cookie.split(";")}
    assert cookie.startswith(f"{SESSION_COOKIE}={KEY};")
    assert {"httponly", "samesite=strict", "path=/", f"max-age={400 * 86400}"} <= attrs
    assert KEY not in r.text
    again = client.get(f"/launch?code={CODE}")
    assert again.status_code == 403
    assert "set-cookie" not in again.headers


@pytest.mark.parametrize("query", ["", "?code=", "?code=wrong", "?code=" + CODE[:-1] + "é"])
def test_launch_wrong_code_403_and_does_not_spend(query):
    client = _client(follow=False)[0]
    r = client.get("/launch" + query)
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert client.get(f"/launch?code={CODE}").status_code == 303


def test_launch_cookie_unlocks_api():
    client = _client()[0]
    client.get(f"/launch?code={CODE}")
    assert client.get("/api/probe").json() == {"ok": True}


def test_ws_accepts_good_cookie_and_origin():
    client, accepted = _client()
    with client.websocket_connect(WS, headers=_cookie() | ORIGIN) as ws:
        assert ws.receive_text() == "hi"
    assert accepted == ["yes"]


@pytest.mark.parametrize(
    "headers",
    [
        _cookie("wrong") | ORIGIN,
        _cookie() | {"origin": "http://evil.example"},
        _cookie(),
    ],
    ids=["bad-cookie", "foreign-origin", "no-origin"],
)
def test_ws_refused_before_accept(headers):
    client, accepted = _client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS, headers=headers):
            pass
    assert accepted == []


def test_ws_foreign_host_refused():
    client, accepted = _client()
    headers = _cookie() | ORIGIN | {"host": "evil.example:8000"}
    with pytest.raises(WebSocketDenialResponse) as e:
        with client.websocket_connect(WS, headers=headers):
            pass
    assert e.value.status_code == 400
    assert accepted == []
