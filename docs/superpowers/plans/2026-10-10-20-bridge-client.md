# Bridge Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python async client for the NinjaTrader add-on, a pure reconciliation function, and an in-process fake add-on that lets every later plan test reconnects, duplicate order IDs, mismatches, dropped connections and stale streams without Windows.

**Architecture:** `fenrir.bridge.names` and `fenrir.bridge.reconcile` are pure. `fenrir.bridge.client.AddonClient` is the thin I/O shell (httpx for REST, `websockets` for the stream) that retries POSTs with the same `client_id` (the add-on dedupes, so retries never double-order) and turns every stream drop or heartbeat gap into a synthetic `{"type": "disconnected"}` message followed by `{"type": "connected"}` on reconnect. `fenrir.bridge.fake_addon` is a FastAPI app implementing the add-on HTTP API from the index, used only by tests.

**Tech Stack:** Python 3.12, httpx, websockets (≥13, `websockets.asyncio.client`), FastAPI + uvicorn (fake add-on), pytest, pytest-asyncio.

**Spec:** docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md (§8 Reconciliation, §9, §12 Bridge)

**Index:** docs/superpowers/plans/README.md (contracts: "Plan 19/20 — add-on HTTP API")

## Global Constraints

- Functional core, imperative shell: `names.py` and `reconcile.py` are pure; only `client.py` and `fake_addon.py` do I/O.
- Every request carries header `X-Fenrir-Token: Settings.nt_token`; the stream uses `?token=`.
- A `client_id` already seen by the add-on is ignored, so retries never double-order (spec §9).
- NinjaTrader is the source of truth for positions (spec §8).
- Instrument names sent to the add-on are NinjaTrader's (`"NQ 12-26"`).
- Instruments: ES, MES, NQ, MNQ only.
- Python 3.12, `uv`; run tests with `uv run pytest -q`.
- Commits: Conventional Commits, scope `bridge`.

## Review Focus

1. **A POST times out after the add-on already placed the order** → the retry must reuse the same `client_id` and come back `duplicate: true`, never a second order. Test: `test_retry_after_timeout_is_deduped` (Task 3).
2. **The add-on stops sending heartbeats but the socket stays open** → the stream must report `disconnected` within `heartbeat_timeout`, not hang forever. Test: `test_missing_heartbeats_mean_disconnected` (Task 3).
3. **Wrong token** → REST fails immediately with 401 (no retries); the stream reports `disconnected` and keeps backing off instead of crashing. Tests: `test_bad_token_is_not_retried`, `test_stream_with_bad_token_reports_disconnected` (Task 3).
4. **A position is open in NinjaTrader but its stop or target is missing or covers fewer contracts** → reconciliation reports it. Tests: `test_open_position_without_stop`, `test_stop_covers_too_few_contracts` (Task 2).
5. **Front-month codes in the wrong shape (one-digit year `NQZ6`, lowercase, unknown month letter)** → a clear `ValueError`, never a wrong instrument name. Test: `test_bad_codes_raise` (Task 1).

---

### Task 1: NinjaTrader instrument names

**Files:**
- Create: `src/fenrir/bridge/__init__.py`
- Create: `src/fenrir/bridge/names.py`
- Create: `src/fenrir/bridge/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/bridge/test_nt_names.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `fenrir.bridge.names.nt_instrument(symbol: str, front_code: str) -> str` (`("MNQ", "NQZ26") -> "MNQ 12-26"`).

- [ ] **Step 1: Write the failing test**

`tests/bridge/test_nt_names.py`:

```python
import pytest

from fenrir.bridge.names import nt_instrument


@pytest.mark.parametrize(
    ("symbol", "code", "expected"),
    [
        ("NQ", "NQZ26", "NQ 12-26"),
        ("MNQ", "NQZ26", "MNQ 12-26"),
        ("ES", "ESH27", "ES 03-27"),
        ("MES", "ESM27", "MES 06-27"),
        ("NQ", "NQU26", "NQ 09-26"),
    ],
)
def test_nt_instrument(symbol, code, expected):
    assert nt_instrument(symbol, code) == expected


@pytest.mark.parametrize("code", ["", "NQ", "NQA26", "NQZ", "NQZ6", "NQZ123", "nqz26"])
def test_bad_codes_raise(code):
    with pytest.raises(ValueError, match="front-month code"):
        nt_instrument("NQ", code)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/bridge/test_nt_names.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.bridge'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/bridge/__init__.py`: empty file.

`src/fenrir/bridge/names.py`:

```python
"""Translate front-month codes ("NQZ26") to NinjaTrader instrument names ("NQ 12-26")."""

import re

_MONTHS = "FGHJKMNQUVXZ"
_CODE = re.compile(r"^[A-Z]+([FGHJKMNQUVXZ])(\d{2})$")  # two-digit year, e.g. "NQZ26"


def nt_instrument(symbol: str, front_code: str) -> str:
    m = _CODE.match(front_code)
    if not m:
        raise ValueError(f"not a front-month code: {front_code!r}")
    return f"{symbol} {_MONTHS.index(m.group(1)) + 1:02d}-{m.group(2)}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/bridge/test_nt_names.py -q`
Expected: `12 passed`

- [ ] **Step 5: Add the directory context file and map row**

`src/fenrir/bridge/CLAUDE.md`:

```markdown
# src/fenrir/bridge/

Talks to the NinjaTrader add-on (`addon/`): REST requests, the event stream, and checking our book against NinjaTrader's.

- Entry points: `client.py` (`AddonClient`: accounts, positions, orders, bracket, flatten, cancel, flatten_all, stream), `reconcile.py` (`reconcile`, `NtPosition`, `NtOrder`, `ExpectedBook`, `Mismatch`), `names.py` (`nt_instrument`).
- Pure vs shell: `names.py`, `reconcile.py` are pure; `client.py` does network I/O; `fake_addon.py` is a test-only fake of the add-on API.
- Not here: when to send orders or halt (that is `src/fenrir/live/`); the C# add-on itself (`addon/`).
- The HTTP/WS contract lives in `docs/superpowers/plans/README.md` ("Plan 19/20"). Change it there first.
- Tests: `tests/bridge/`.
```

In root `CLAUDE.md`, add this row to the `## 6. Directory Map` table (after the last existing row):

```markdown
| `src/fenrir/bridge/` | Python side of the NinjaTrader link: add-on client, reconciliation, fake add-on for tests. |
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/bridge/__init__.py src/fenrir/bridge/names.py src/fenrir/bridge/CLAUDE.md tests/bridge/test_nt_names.py CLAUDE.md
git commit -m "feat(bridge): map front-month codes to ninjatrader instrument names

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Reconciliation

**Files:**
- Create: `src/fenrir/bridge/reconcile.py`
- Test: `tests/bridge/test_reconcile.py`

**Interfaces:**
- Consumes: nothing.
- Produces (used by plans 22, 23):

```python
@dataclass(frozen=True, slots=True)
class NtPosition: account: str; instrument: str; qty: int; avg_price: float
@dataclass(frozen=True, slots=True)
class NtOrder: account: str; order_id: str; client_id: str; instrument: str; role: str; action: str; qty: int; price: float; state: str
@dataclass(frozen=True, slots=True)
class ExpectedBook: account: str; instrument: str; qty: int      # signed qty the runner believes it holds
@dataclass(frozen=True, slots=True)
class Mismatch: kind: str; detail: str   # kind ∈ "position" | "unexpected_position" | "missing_stop" | "missing_target" | "stray_orders"
def reconcile(expected: ExpectedBook, positions: Sequence[NtPosition], orders: Sequence[NtOrder]) -> tuple[Mismatch, ...]
```

`orders` are working orders only (the add-on's `/orders` returns working orders). Rows for other accounts are ignored.

- [ ] **Step 1: Write the failing tests**

`tests/bridge/test_reconcile.py`:

```python
from fenrir.bridge.reconcile import ExpectedBook, NtOrder, NtPosition, reconcile

NQ = "NQ 12-26"


def pos(qty, instrument=NQ, account="Sim101"):
    return NtPosition(account=account, instrument=instrument, qty=qty, avg_price=20000.0)


def order(role, qty=1, instrument=NQ, account="Sim101", order_id="o1"):
    return NtOrder(
        account=account, order_id=order_id, client_id="c1", instrument=instrument,
        role=role, action="sell", qty=qty, price=19990.0, state="Working",
    )


def kinds(mismatches):
    return [m.kind for m in mismatches]


def test_flat_and_no_orders_matches():
    assert reconcile(ExpectedBook("Sim101", NQ, 0), [], []) == ()


def test_protected_position_matches():
    expected = ExpectedBook("Sim101", NQ, 1)
    assert reconcile(expected, [pos(1)], [order("stop"), order("target", order_id="o2")]) == ()


def test_quantity_differs():
    assert kinds(reconcile(ExpectedBook("Sim101", NQ, 0), [pos(2)], [order("stop", 2), order("target", 2)])) == [
        "position"
    ]


def test_position_on_other_instrument():
    result = reconcile(ExpectedBook("Sim101", NQ, 0), [pos(1, instrument="ES 12-26")], [])
    assert kinds(result) == ["unexpected_position"]


def test_open_position_without_stop():
    result = reconcile(ExpectedBook("Sim101", NQ, 1), [pos(1)], [order("target")])
    assert kinds(result) == ["missing_stop"]


def test_stop_covers_too_few_contracts():
    result = reconcile(ExpectedBook("Sim101", NQ, -3), [pos(-3)], [order("stop", 1), order("target", 3)])
    assert kinds(result) == ["missing_stop"]


def test_flat_with_working_orders():
    result = reconcile(ExpectedBook("Sim101", NQ, 0), [], [order("stop")])
    assert kinds(result) == ["stray_orders"]
    assert "o1" in result[0].detail


def test_other_accounts_are_ignored():
    assert reconcile(ExpectedBook("Sim101", NQ, 0), [pos(1, account="Sim102")], [order("stop", account="Sim102")]) == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/bridge/test_reconcile.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.bridge.reconcile'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/bridge/reconcile.py`:

```python
"""Compare what the runner believes it holds with NinjaTrader's positions and working orders. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NtPosition:
    account: str
    instrument: str
    qty: int  # signed
    avg_price: float


@dataclass(frozen=True, slots=True)
class NtOrder:
    account: str
    order_id: str
    client_id: str
    instrument: str
    role: str  # "entry" | "stop" | "target" | "other"
    action: str  # "buy" | "sell"
    qty: int
    price: float
    state: str


@dataclass(frozen=True, slots=True)
class ExpectedBook:
    account: str
    instrument: str
    qty: int  # signed


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str
    detail: str


def reconcile(
    expected: ExpectedBook, positions: Sequence[NtPosition], orders: Sequence[NtOrder]
) -> tuple[Mismatch, ...]:
    mine = [p for p in positions if p.account == expected.account and p.qty != 0]
    working = [o for o in orders if o.account == expected.account]
    out = [
        Mismatch("unexpected_position", f"{p.instrument} qty {p.qty}")
        for p in mine
        if p.instrument != expected.instrument
    ]
    held = sum(p.qty for p in mine if p.instrument == expected.instrument)
    if held != expected.qty:
        out.append(
            Mismatch("position", f"runner expects {expected.qty} {expected.instrument}, NinjaTrader has {held}")
        )
    if held:
        for role in ("stop", "target"):
            covered = sum(o.qty for o in working if o.role == role and o.instrument == expected.instrument)
            if covered != abs(held):
                out.append(Mismatch(f"missing_{role}", f"{role} orders cover {covered} of {abs(held)} contracts"))
    elif working:
        out.append(Mismatch("stray_orders", "working orders while flat: " + ", ".join(o.order_id for o in working)))
    return tuple(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/bridge/test_reconcile.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/bridge/reconcile.py tests/bridge/test_reconcile.py
git commit -m "feat(bridge): reconcile runner book against ninjatrader positions and orders

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Fake add-on and async client

**Files:**
- Create: `src/fenrir/bridge/fake_addon.py`
- Create: `src/fenrir/bridge/client.py`
- Test: `tests/bridge/test_addon_client.py`

**Interfaces:**
- Consumes: `NtPosition`, `NtOrder` (Task 2).
- Produces (used by plans 22, 23, 24):

```python
@dataclass(frozen=True, slots=True)
class Ack: accepted: bool; duplicate: bool; error: str | None   # duplicate=True means an earlier attempt already landed: treat as success

class AddonClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0, retries: int = 3,
                 heartbeat_timeout: float = 6.0, reconnect_delay: float = 1.0) -> None
    async def accounts(self) -> list[dict]
    async def positions(self, account: str) -> tuple[NtPosition, ...]
    async def orders(self, account: str) -> tuple[NtOrder, ...]
    async def bracket(self, client_id: str, account: str, instrument: str, side: str, qty: int, stop: float, target: float) -> Ack
    async def flatten(self, client_id: str, account: str) -> Ack
    async def cancel(self, client_id: str, account: str, order_id: str) -> Ack
    async def flatten_all(self, client_id: str) -> Ack
    def stream(self) -> AsyncIterator[dict]   # never ends; yields {"type":"connected"} after each (re)connect and
                                              # {"type":"disconnected","error":str} on drop, bad token or heartbeat gap
    async def aclose(self) -> None

# test support (fenrir.bridge.fake_addon)
@dataclass
class FakeAddon:
    token: str = "secret"
    accounts: list[dict]; positions: list[dict]; orders: list[dict]
    requests: list[tuple[str, dict]]   # every POST (path, body) in arrival order
    seen: set[str]                     # client_ids already handled
    delays: list[float]                # per-POST response delay, popped in order (after processing)
    heartbeats: bool = True
    heartbeat_every: float = 2.0
    def push(self, msg: dict) -> None  # send a stream message
    def kick(self) -> None             # close the current stream socket
def make_app(fake: FakeAddon) -> FastAPI
def running(fake: FakeAddon) -> AsyncContextManager[str]   # yields base URL "http://127.0.0.1:<port>"
```

- [ ] **Step 1: Add dependencies**

Run: `uv add httpx websockets fastapi "uvicorn[standard]"` and `uv add --dev pytest-asyncio`
Expected: `pyproject.toml` lists them (no-op for any already present).

- [ ] **Step 2: Write the failing tests**

`tests/bridge/test_addon_client.py`:

```python
import asyncio

import httpx
import pytest
import pytest_asyncio

from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.bridge.reconcile import NtOrder, NtPosition

ACCOUNT = {"id": "Sim101", "name": "Sim101", "is_sim": True, "balance": 50000.0,
           "realized_pnl_today": 0.0, "unrealized_pnl": 0.0}
POSITION = {"account": "Sim101", "instrument": "NQ 12-26", "qty": 1, "avg_price": 20000.0}
ORDER = {"account": "Sim101", "order_id": "o1", "client_id": "c1", "instrument": "NQ 12-26",
         "role": "stop", "action": "sell", "qty": 1, "price": 19990.0, "state": "Working"}


@pytest_asyncio.fixture
async def addon():
    fake = FakeAddon(accounts=[ACCOUNT], positions=[POSITION], orders=[ORDER], heartbeat_every=0.05)
    async with running(fake) as url:
        client = AddonClient(url, fake.token, timeout=0.3, heartbeat_timeout=0.3, reconnect_delay=0.05)
        yield fake, client
        await client.aclose()


async def until(gen, pred, timeout=3.0):
    seen = []

    async def run():
        async for msg in gen:
            seen.append(msg)
            if pred(msg):
                return

    await asyncio.wait_for(run(), timeout)
    return seen


@pytest.mark.asyncio
async def test_reads(addon):
    _, client = addon
    assert await client.accounts() == [ACCOUNT]
    assert await client.positions("Sim101") == (NtPosition("Sim101", "NQ 12-26", 1, 20000.0),)
    assert await client.orders("Sim101") == (
        NtOrder("Sim101", "o1", "c1", "NQ 12-26", "stop", "sell", 1, 19990.0, "Working"),
    )
    assert await client.positions("Other") == ()


@pytest.mark.asyncio
async def test_bracket_is_sent_once(addon):
    fake, client = addon
    ack = await client.bracket("c9", "Sim101", "NQ 12-26", "long", 1, 19990.0, 20020.0)
    assert (ack.accepted, ack.duplicate, ack.error) == (True, False, None)
    assert fake.requests == [("/bracket", {"client_id": "c9", "account": "Sim101", "instrument": "NQ 12-26",
                                           "side": "long", "qty": 1, "stop": 19990.0, "target": 20020.0})]


@pytest.mark.asyncio
async def test_retry_after_timeout_is_deduped(addon):
    fake, client = addon
    fake.delays = [1.0]  # first response arrives after the client gave up
    ack = await client.flatten("c10", "Sim101")
    assert ack.duplicate is True
    assert [p for p, _ in fake.requests] == ["/flatten", "/flatten"]
    assert fake.seen == {"c10"}


@pytest.mark.asyncio
async def test_bad_token_is_not_retried(addon):
    fake, _ = addon
    async with running(fake) as url:
        client = AddonClient(url, "wrong", timeout=0.3)
        with pytest.raises(httpx.HTTPStatusError) as err:
            await client.cancel("c11", "Sim101", "o1")
        await client.aclose()
    assert err.value.response.status_code == 401
    assert fake.requests == []


@pytest.mark.asyncio
async def test_stream_yields_connected_then_events(addon):
    fake, client = addon
    fill = {"type": "fill", "account": "Sim101", "client_id": "c1", "order_id": "o1",
            "instrument": "NQ 12-26", "qty": 1, "price": 20000.25, "commission": 0.0,
            "ts": "2026-10-12T13:31:00+00:00"}
    gen = client.stream()
    first = await until(gen, lambda m: True)
    assert first == [{"type": "connected"}]
    fake.push(fill)
    seen = await until(gen, lambda m: m["type"] == "fill")
    assert seen[-1] == fill
    await gen.aclose()


@pytest.mark.asyncio
async def test_drop_then_reconnect(addon):
    fake, client = addon
    gen = client.stream()
    await until(gen, lambda m: m["type"] == "heartbeat")
    fake.kick()
    seen = await until(gen, lambda m: m["type"] == "connected")
    assert [m["type"] for m in seen if m["type"] != "heartbeat"] == ["disconnected", "connected"]
    await gen.aclose()


@pytest.mark.asyncio
async def test_missing_heartbeats_mean_disconnected(addon):
    fake, client = addon
    gen = client.stream()
    await until(gen, lambda m: m["type"] == "heartbeat")
    fake.heartbeats = False
    seen = await until(gen, lambda m: m["type"] == "disconnected")
    assert seen[-1]["type"] == "disconnected"
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_with_bad_token_reports_disconnected(addon):
    fake, _ = addon
    async with running(fake) as url:
        client = AddonClient(url, "wrong", reconnect_delay=0.05)
        gen = client.stream()
        seen = await until(gen, lambda m: True)
        assert seen[0]["type"] == "disconnected"
        await gen.aclose()
        await client.aclose()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/bridge/test_addon_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.bridge.client'`

- [ ] **Step 4: Write the fake add-on**

`src/fenrir/bridge/fake_addon.py`:

```python
"""In-process fake of the NinjaTrader add-on HTTP/WS API. Test support only; mutable on purpose."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket


@dataclass
class FakeAddon:
    token: str = "secret"
    accounts: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    requests: list[tuple[str, dict]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    delays: list[float] = field(default_factory=list)
    heartbeats: bool = True
    heartbeat_every: float = 2.0
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)

    def push(self, msg: dict) -> None:
        self.outbox.put_nowait(msg)

    def kick(self) -> None:
        self.push({"type": "__close__"})


def make_app(fake: FakeAddon) -> FastAPI:
    app = FastAPI()

    def auth(x_fenrir_token: str | None = Header(default=None)) -> None:
        if x_fenrir_token != fake.token:
            raise HTTPException(status_code=401)

    deps = [Depends(auth)]

    @app.get("/accounts", dependencies=deps)
    async def accounts() -> list[dict]:
        return fake.accounts

    @app.get("/positions", dependencies=deps)
    async def positions(account: str) -> list[dict]:
        return [p for p in fake.positions if p["account"] == account]

    @app.get("/orders", dependencies=deps)
    async def orders(account: str) -> list[dict]:
        return [o for o in fake.orders if o["account"] == account]

    async def post(request: Request) -> dict:
        body = await request.json()
        fake.requests.append((request.url.path, body))
        duplicate = body["client_id"] in fake.seen
        fake.seen.add(body["client_id"])
        if fake.delays:
            await asyncio.sleep(fake.delays.pop(0))  # processed already; the client may time out first
        return {"accepted": not duplicate, "duplicate": duplicate, "error": None}

    for path in ("/bracket", "/flatten", "/cancel", "/flatten-all"):
        app.add_api_route(path, post, methods=["POST"], dependencies=deps)

    @app.websocket("/stream")
    async def stream(ws: WebSocket, token: str = "") -> None:
        if token != fake.token:
            await ws.close(code=4401)
            return
        await ws.accept()
        while True:
            try:
                msg = await asyncio.wait_for(fake.outbox.get(), fake.heartbeat_every)
            except TimeoutError:
                if not fake.heartbeats:
                    continue
                msg = {"type": "heartbeat", "ts": datetime.now(UTC).isoformat()}
            if msg.get("type") == "__close__":
                await ws.close()
                return
            await ws.send_json(msg)

    return app


@asynccontextmanager
async def running(fake: FakeAddon) -> AsyncIterator[str]:
    server = uvicorn.Server(uvicorn.Config(make_app(fake), host="127.0.0.1", port=0, log_level="warning"))
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
```

- [ ] **Step 5: Write the client**

`src/fenrir/bridge/client.py`:

```python
"""Async client for the NinjaTrader add-on. Thin I/O shell; POST retries reuse the client_id so the add-on dedupes."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from fenrir.bridge.reconcile import NtOrder, NtPosition


@dataclass(frozen=True, slots=True)
class Ack:
    accepted: bool
    duplicate: bool
    error: str | None


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
        self._http = httpx.AsyncClient(base_url=base_url, headers={"X-Fenrir-Token": token}, timeout=timeout)
        self._ws_url = "ws" + base_url.rstrip("/").removeprefix("http") + "/stream?token=" + quote(token)
        self._retries = retries
        self._heartbeat_timeout = heartbeat_timeout
        self._reconnect_delay = reconnect_delay

    async def _get(self, path: str, **params: str) -> list[dict]:
        r = await self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> Ack:
        for attempt in range(self._retries):
            try:
                r = await self._http.post(path, json=body)
            except httpx.TransportError:
                if attempt == self._retries - 1:
                    raise
                await asyncio.sleep(0.1 * 2**attempt)
                continue
            r.raise_for_status()
            return Ack(**r.json())
        raise AssertionError("unreachable")

    async def accounts(self) -> list[dict]:
        return await self._get("/accounts")

    async def positions(self, account: str) -> tuple[NtPosition, ...]:
        return tuple(NtPosition(**p) for p in await self._get("/positions", account=account))

    async def orders(self, account: str) -> tuple[NtOrder, ...]:
        return tuple(NtOrder(**o) for o in await self._get("/orders", account=account))

    async def bracket(
        self, client_id: str, account: str, instrument: str, side: str, qty: int, stop: float, target: float
    ) -> Ack:
        return await self._post("/bracket", {"client_id": client_id, "account": account, "instrument": instrument,
                                             "side": side, "qty": qty, "stop": stop, "target": target})

    async def flatten(self, client_id: str, account: str) -> Ack:
        return await self._post("/flatten", {"client_id": client_id, "account": account})

    async def cancel(self, client_id: str, account: str, order_id: str) -> Ack:
        return await self._post("/cancel", {"client_id": client_id, "account": account, "order_id": order_id})

    async def flatten_all(self, client_id: str) -> Ack:
        return await self._post("/flatten-all", {"client_id": client_id})

    async def stream(self) -> AsyncIterator[dict]:
        delay = self._reconnect_delay
        while True:
            try:
                async with connect(self._ws_url) as ws:
                    delay = self._reconnect_delay
                    yield {"type": "connected"}
                    while True:
                        # no message (not even a heartbeat) within the timeout = stale link
                        yield json.loads(await asyncio.wait_for(ws.recv(), self._heartbeat_timeout))
            except (OSError, TimeoutError, WebSocketException) as e:
                yield {"type": "disconnected", "error": repr(e)}
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def aclose(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/bridge -q`
Expected: `28 passed` (12 names + 8 reconcile + 8 client)

If `test_drop_then_reconnect` or `test_missing_heartbeats_mean_disconnected` hang, check that uvicorn uses the `websockets` backend (`uv pip show websockets`) and that `heartbeat_timeout` (0.3 s) exceeds `heartbeat_every` (0.05 s).

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/fenrir/bridge tests/bridge && uv run ruff format --check src/fenrir/bridge tests/bridge`
Expected: no errors (run `uv run ruff format src/fenrir/bridge tests/bridge` if format check fails).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/fenrir/bridge/fake_addon.py src/fenrir/bridge/client.py tests/bridge/test_addon_client.py
git commit -m "feat(bridge): add async add-on client with deduped retries and a fake add-on

Stream reports disconnected on drops, bad tokens and heartbeat gaps,
then reconnects with backoff so the runner can reconcile each time.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
