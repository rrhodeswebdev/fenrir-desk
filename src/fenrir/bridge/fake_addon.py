"""In-process fake of the NinjaTrader add-on HTTP/WS API (docs/contracts.md, "Plan 19/20").

Test support only, mutable on purpose: tests set its state, push stream messages, kick the socket.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket

_CLOSE = {"type": "__close__"}


def now_ts() -> str:
    """The add-on's timestamp shape: UTC, milliseconds, Z ("2026-10-12T13:31:00.250Z")."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class FakeAddon:
    token: str = "secret"
    accounts: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # every POST, in order
    seen: set[str] = field(default_factory=set)  # client_ids already handled
    delays: list[float] = field(default_factory=list)  # per-POST delay after handling, popped
    rejects: list[str] = field(
        default_factory=list
    )  # per-POST refusal error, popped (not duplicates)
    heartbeats: bool = True
    heartbeat_every: float = 2.0
    outbox: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    def push(self, msg: dict[str, Any]) -> None:
        """Send a message on the current stream socket."""
        self.outbox.put_nowait(msg)

    def kick(self) -> None:
        """Close the current stream socket."""
        self.push(_CLOSE)


def make_app(fake: FakeAddon) -> FastAPI:
    app = FastAPI()

    def auth(x_fenrir_token: str | None = Header(default=None)) -> None:
        if x_fenrir_token != fake.token:
            raise HTTPException(status_code=401)

    deps = [Depends(auth)]

    @app.get("/accounts", dependencies=deps)
    async def accounts() -> list[dict[str, Any]]:
        return fake.accounts

    @app.get("/positions", dependencies=deps)
    async def positions(account: str) -> list[dict[str, Any]]:
        return [p for p in fake.positions if p["account"] == account]

    @app.get("/orders", dependencies=deps)
    async def orders(account: str) -> list[dict[str, Any]]:
        return [o for o in fake.orders if o["account"] == account]

    async def post(request: Request) -> dict[str, Any]:
        body = await request.json()
        fake.requests.append((request.url.path, body))
        duplicate = body["client_id"] in fake.seen
        fake.seen.add(body["client_id"])  # as the add-on, a refused id is seen too
        error = fake.rejects.pop(0) if fake.rejects and not duplicate else None
        if fake.delays:
            await asyncio.sleep(
                fake.delays.pop(0)
            )  # handled already; the client may time out first
        return {"accepted": not duplicate and error is None, "duplicate": duplicate, "error": error}

    for path in ("/bracket", "/flatten", "/cancel", "/flatten-all"):
        app.add_api_route(path, post, methods=["POST"], dependencies=deps)

    @app.websocket("/stream")
    async def stream(ws: WebSocket, token: str = "") -> None:
        if token != fake.token:
            await ws.close()  # before accept: the handshake is refused
            return
        await ws.accept()
        outbox = fake.outbox = asyncio.Queue()  # pushes go to the newest socket only
        gone = asyncio.create_task(ws.receive())  # the client sends nothing; this ends on close
        try:
            while True:
                get = asyncio.create_task(outbox.get())
                await asyncio.wait(
                    {get, gone}, timeout=fake.heartbeat_every, return_when="FIRST_COMPLETED"
                )
                if gone.done():
                    get.cancel()
                    return
                if get.done():
                    msg = get.result()
                else:
                    get.cancel()
                    if not fake.heartbeats:
                        continue
                    msg = {"type": "heartbeat", "ts": now_ts()}
                if msg is _CLOSE:
                    await ws.close()
                    return
                await ws.send_json(msg)
        finally:
            gone.cancel()

    return app


@asynccontextmanager
async def running(fake: FakeAddon) -> AsyncIterator[str]:
    """Serve the fake on a free loopback port; yields its base URL."""
    config = uvicorn.Config(
        make_app(fake),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = server.force_exit = True
        await task
