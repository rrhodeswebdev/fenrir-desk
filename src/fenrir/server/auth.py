"""Session-cookie, Origin and CSRF checks. Pure predicates first; FastAPI glue last.

The glue reads `app.state.session_key: str` and `app.state.allowed_origins: frozenset[str]`.
"""

import secrets

from fastapi import HTTPException, Request, WebSocket

SESSION_COOKIE = "fenrir_session"
VITE_PORT = 5173


def session_ok(expected: str, given: str | None) -> bool:
    # bytes: compare_digest raises TypeError on non-ASCII str, and cookies are attacker-chosen
    return (
        bool(expected)
        and given is not None
        and secrets.compare_digest(expected.encode(), given.encode())
    )


def allowed_origins(port: int, dev: bool) -> frozenset[str]:
    ports = (port, VITE_PORT) if dev else (port,)
    return frozenset(f"http://{host}:{p}" for host in ("127.0.0.1", "localhost") for p in ports)


def origin_ok(origin: str | None, allowed: frozenset[str]) -> bool:
    return origin in allowed


def csrf_ok(
    method: str, origin: str | None, sec_fetch_site: str | None, allowed: frozenset[str]
) -> bool:
    # SameSite does not separate localhost ports, so writes must prove they come from our page
    return method == "GET" or (
        origin_ok(origin, allowed) and sec_fetch_site in (None, "same-origin")
    )


def require_session(request: Request) -> None:
    state = request.app.state
    if not session_ok(state.session_key, request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="missing or bad session")
    headers = request.headers
    if not csrf_ok(
        request.method, headers.get("origin"), headers.get("sec-fetch-site"), state.allowed_origins
    ):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def check_ws(ws: WebSocket) -> bool:
    state = ws.app.state
    return session_ok(state.session_key, ws.cookies.get(SESSION_COOKIE)) and origin_ok(
        ws.headers.get("origin"), state.allowed_origins
    )
