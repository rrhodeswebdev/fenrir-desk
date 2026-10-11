"""The FastAPI app: Host guard, launch link, health and the SPA. Pure helpers first; glue last."""

import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from fenrir.server.auth import SESSION_COOKIE, allowed_origins, session_ok
from fenrir.settings import Settings

COOKIE_MAX_AGE = 400 * 86400  # browsers cap cookie lifetime at 400 days
_HOST = re.compile(r"(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")


def host_action(host: str | None) -> Literal["ok", "redirect", "reject"]:
    # only loopback (DNS rebinding); 127.0.0.1 moves to localhost so the cookie has one host
    m = _HOST.fullmatch(host or "")
    return "reject" if m is None else "ok" if m[1] == "localhost" else "redirect"


def localhost_url(host: str, raw_path: bytes, query: bytes) -> str:
    port = host.removeprefix("127.0.0.1")
    return f"http://localhost{port}{raw_path.decode('latin-1')}" + (
        f"?{query.decode('latin-1')}" if query else ""
    )


def static_file(root: Path, path: str) -> Path | None:
    """The file `path` names inside the resolved `root`, or None if missing or outside it."""
    if "\x00" in path:  # resolve() raises on NUL
        return None
    file = (root / path).resolve()
    return file if file.is_file() and file.is_relative_to(root) else None


class HostGuard:
    """Runs before routing, for every HTTP and WebSocket request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        host = Headers(scope=scope).get("host")
        action = host_action(host)
        if action == "ok":
            return await self.app(scope, receive, send)
        # on a WebSocket, starlette sends these as a handshake denial, never an accept
        response = (
            RedirectResponse(
                localhost_url(host or "", scope.get("raw_path") or b"", scope["query_string"]),
                status_code=307,
            )
            if action == "redirect"
            else PlainTextResponse("invalid host header", status_code=400)
        )
        await response(scope, receive, send)


def create_app(
    settings: Settings,
    session_key: str,
    launch_code: str,
    static_dir: Path | None,
    *,
    port: int = 8000,
    dev: bool = False,
) -> FastAPI:
    app = FastAPI(title="Fenrir Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.session_key = session_key
    app.state.allowed_origins = allowed_origins(port, dev)
    app.add_middleware(HostGuard)
    unspent = [launch_code]

    @app.get("/launch", include_in_schema=False)
    async def launch(code: str = "") -> Response:
        # async with no await: the check and the spend cannot interleave with another request
        if not unspent or not session_ok(unspent[0], code):
            raise HTTPException(status_code=403, detail="launch link is wrong or already used")
        unspent.clear()
        # relative, so a Vite dev proxy on :5173 stays on :5173
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_key,
            max_age=COOKIE_MAX_AGE,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    # Register routers here, one line each, above the SPA:
    # app.include_router(<area>.router, dependencies=[Depends(require_session)])

    if static_dir is not None:
        _mount_spa(app, static_dir.resolve())

    return app


def _mount_spa(app: FastAPI, root: Path) -> None:
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.split("/")[0] in ("api", "ws"):
            raise HTTPException(status_code=404)
        return FileResponse(static_file(root, path) or root / "index.html")
