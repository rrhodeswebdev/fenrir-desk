"""Async client for the NinjaTrader add-on (API in docs/contracts.md, "Plan 19/20").

Pure parsing of the add-on's untrusted JSON (TypedDicts checked by pydantic) and a thin I/O shell.
POST retries reuse the body, so the add-on's client_id dedupe turns a retry into `duplicate`.
The token never reaches a log or an error: websockets' DEBUG handshake log is cut (the token rides
in the stream URL) and stream errors are redacted.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, TypedDict
from urllib.parse import quote

import httpx
from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

_STRICT = ConfigDict(strict=True, allow_inf_nan=False)
# UTC ISO 8601 with milliseconds and Z, e.g. "2026-10-12T13:31:00.250Z"
Ts = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")]

_WS_LOG = logging.getLogger("fenrir.bridge.client.ws")
# websockets logs the handshake request line, token included, at DEBUG; an explicit level outlives
# dictConfig (uvicorn's resets `disabled`) and a DEBUG root
_WS_LOG.setLevel(logging.INFO)


@dataclass(frozen=True, config=_STRICT)
class Ack:
    accepted: bool
    duplicate: bool  # True: an earlier attempt landed; treat as success
    error: str | None


class Account(TypedDict):
    id: str
    name: str
    is_sim: bool
    balance: float
    realized_pnl_today: float
    unrealized_pnl: float


class Position(TypedDict):
    account: str
    instrument: str
    qty: int  # signed
    avg_price: float


class Order(TypedDict):
    account: str
    order_id: str
    client_id: str
    instrument: str
    role: Literal["entry", "stop", "target", "other"]
    action: Literal["buy", "sell"]
    qty: int
    price: float
    state: str


class Heartbeat(TypedDict):
    type: Literal["heartbeat"]
    ts: Ts


class AccountEvent(Account):
    type: Literal["account"]


class PositionEvent(Position):
    type: Literal["position"]


class OrderEvent(Order):
    type: Literal["order"]


class Fill(TypedDict):
    type: Literal["fill"]
    account: str
    client_id: str
    order_id: str
    instrument: str
    qty: int  # signed: + buy, - sell
    price: float
    commission: float
    ts: Ts


Event = Annotated[
    Heartbeat | AccountEvent | PositionEvent | OrderEvent | Fill, Field(discriminator="type")
]

ACCOUNTS = TypeAdapter(list[Account], config=_STRICT)
POSITIONS = TypeAdapter(list[Position], config=_STRICT)
ORDERS = TypeAdapter(list[Order], config=_STRICT)
ACK = TypeAdapter(Ack)
EVENT = TypeAdapter(Event, config=_STRICT)


def parse[T](adapter: TypeAdapter[T], raw: str | bytes, what: str) -> T:
    """Validate add-on JSON, or raise ValueError naming the first problem's place and kind
    (pydantic's `msg` can quote the input, e.g. an unknown `type` tag, so it is left out)."""
    try:
        return adapter.validate_json(raw)
    except ValidationError as e:
        first = e.errors(include_url=False, include_input=False)[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ValueError(
            f"malformed {what}: {e.error_count()} error(s), first at {loc}: {first['type']}"
        ) from None


def redact(text: str, token: str) -> str:
    for secret in {token, quote(token, safe="")} - {""}:
        text = text.replace(secret, "***")
    return text


class AddonClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
        retries: int = 3,
        heartbeat_timeout: float = 6.0,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Fenrir-Token": token},
            timeout=timeout,
            trust_env=False,  # never via an env proxy: it would see the token and every order
        )
        base = base_url.rstrip("/")
        self._ws_url = "ws" + base.removeprefix("http") + "/stream?token=" + quote(token, safe="")
        self._token = token
        self._timeout = timeout
        self._retries = retries
        self._heartbeat_timeout = heartbeat_timeout
        self._reconnect_delay = reconnect_delay

    async def _get[T](self, adapter: TypeAdapter[T], path: str, **params: str) -> T:
        r = await self._http.get(path, params=params)
        r.raise_for_status()  # 401 and unreachable raise httpx.HTTPError, never retried
        return parse(adapter, r.content, f"{path} response")

    async def _post(self, path: str, body: dict[str, Any]) -> Ack:
        for attempt in range(self._retries):
            try:
                r = await self._http.post(path, json=body)
            except httpx.TransportError:  # timeouts included: the order may have landed
                if attempt == self._retries - 1:
                    raise
                await asyncio.sleep(0.1 * 2**attempt)
                continue
            r.raise_for_status()
            return parse(ACK, r.content, f"{path} response")
        raise ValueError("retries must be at least 1")

    async def accounts(self) -> list[Account]:
        return await self._get(ACCOUNTS, "/accounts")

    async def positions(self, account: str) -> list[Position]:
        return await self._get(POSITIONS, "/positions", account=account)

    async def orders(self, account: str) -> list[Order]:
        return await self._get(ORDERS, "/orders", account=account)

    async def bracket(
        self,
        client_id: str,
        account: str,
        instrument: str,
        side: Literal["long", "short"],
        qty: int,
        stop: float,
        target: float,
    ) -> Ack:
        body = {"client_id": client_id, "account": account, "instrument": instrument}
        return await self._post(
            "/bracket", body | {"side": side, "qty": qty, "stop": stop, "target": target}
        )

    async def flatten(self, client_id: str, account: str) -> Ack:
        return await self._post("/flatten", {"client_id": client_id, "account": account})

    async def cancel(self, client_id: str, account: str, order_id: str) -> Ack:
        body = {"client_id": client_id, "account": account, "order_id": order_id}
        return await self._post("/cancel", body)

    async def flatten_all(self, client_id: str) -> Ack:
        return await self._post("/flatten-all", {"client_id": client_id})

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Never ends. Yields {"type": "connected"} after each (re)connect, then validated events;
        {"type": "disconnected", "error": str} on a drop, bad token, malformed message or no
        message at all for heartbeat_timeout; then reconnects with backoff (capped at 30 s)."""
        delay = self._reconnect_delay
        while True:
            try:
                async with connect(
                    self._ws_url,
                    logger=_WS_LOG,
                    open_timeout=self._timeout,
                    close_timeout=1.0,  # a dead peer never answers the close; don't wait 10 s
                    proxy=None,
                ) as ws:
                    yield {"type": "connected"}
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), self._heartbeat_timeout)
                        msg = parse(EVENT, raw, "stream message")
                        delay = self._reconnect_delay  # back off from peers that accept then fail
                        yield msg
            except TimeoutError:
                yield {"type": "disconnected", "error": "no heartbeat or open timed out"}
            except (OSError, ValueError, WebSocketException) as e:
                error = redact(f"{type(e).__name__}: {e}", self._token)
                yield {"type": "disconnected", "error": error}
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def aclose(self) -> None:
        await self._http.aclose()
