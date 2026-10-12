"""The FastAPI app: Host guard, launch link, health and the SPA. Pure helpers first; glue last."""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from fenrir.live.service import LiveHub
from fenrir.server.auth import SESSION_COOKIE, allowed_origins, require_session, session_ok
from fenrir.server.jobs_runner import JobRunner
from fenrir.server.pull import PullRunner, pull_command, pull_env
from fenrir.server.routes import backtests, data, live, rules, strategies, terminal
from fenrir.server.routes import settings as settings_routes
from fenrir.settings import Settings

COOKIE_MAX_AGE = 400 * 86400  # browsers cap cookie lifetime at 400 days
_HOST = re.compile(r"(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")


def host_action(host: str | None) -> Literal["ok", "redirect", "reject"]:
    # only loopback (DNS rebinding); 127.0.0.1 moves to localhost so the cookie has one host
    m = _HOST.fullmatch(host or "")
    return "reject" if m is None else "ok" if m[1] == "localhost" else "redirect"


def localhost_url(host: str, raw_path: bytes, path: str, query: bytes) -> str:
    port = host.removeprefix("127.0.0.1")
    # an absolute-form target (GET http://h/p) puts the whole URL in raw_path; keep only the path
    target = raw_path.decode("latin-1") if raw_path.startswith(b"/") else path
    return f"http://localhost{port}{target}" + (f"?{query.decode('latin-1')}" if query else "")


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
        hosts = Headers(scope=scope).getlist("host")
        # a repeated Host is refused: servers differ on which copy wins
        host = hosts[0] if len(hosts) == 1 else None
        action = host_action(host)
        if action == "ok":
            return await self.app(scope, receive, send)
        # on a WebSocket, starlette sends these as a handshake denial, never an accept
        response = (
            RedirectResponse(
                localhost_url(
                    host or "", scope.get("raw_path") or b"", scope["path"], scope["query_string"]
                ),
                status_code=307,
            )
            if action == "redirect"
            else PlainTextResponse("invalid host header", status_code=400)
        )
        await response(scope, receive, send)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # the job and step children run in their own sessions, so they would outlive a server that
    # never stops them
    app.state.jobs.start()
    app.state.live.start()
    try:
        yield
    finally:
        try:
            await app.state.live.stop()
        finally:
            app.state.jobs.stop()


def create_app(
    settings: Settings,
    session_key: str,
    launch_code: str,
    static_dir: Path | None,
    *,
    port: int = 8000,
    dev: bool = False,
) -> FastAPI:
    app = FastAPI(
        title="Fenrir Desk", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan
    )
    app.state.settings = settings
    app.state.session_key = session_key
    app.state.allowed_origins = allowed_origins(port, dev)
    app.state.pull = PullRunner(pull_command(), pull_env(os.environ, settings), settings=settings)
    app.state.jobs = JobRunner(settings, secret=session_key)  # started by _lifespan
    app.state.live = LiveHub(settings, secret=session_key)  # opens nothing; started by _lifespan
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
    app.include_router(rules.router, dependencies=[Depends(require_session)])
    app.include_router(data.router, dependencies=[Depends(require_session)])
    app.include_router(strategies.router, dependencies=[Depends(require_session)])
    app.include_router(backtests.router, dependencies=[Depends(require_session)])
    app.include_router(settings_routes.router, dependencies=[Depends(require_session)])
    app.include_router(live.router, dependencies=[Depends(require_session)])
    app.include_router(backtests.ws_router)  # check_ws in the handler, before accept()
    app.include_router(terminal.router)  # WebSockets call check_ws themselves, before accept()

    if static_dir is not None:
        _mount_spa(app, static_dir.resolve())

    return app


def _mount_spa(app: FastAPI, root: Path) -> None:
    if not (root / "index.html").is_file():
        raise ValueError(f"{root} has no index.html; build the web UI first")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.split("/")[0] in ("api", "ws"):
            raise HTTPException(status_code=404)
        return FileResponse(static_file(root, path) or root / "index.html")
