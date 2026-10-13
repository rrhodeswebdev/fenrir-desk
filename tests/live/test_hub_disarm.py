"""#223: disarming a sim or live account flattens it through the add-on (`/flatten`, which also
cancels its working orders) before the row is gone, in the kill switch's order: once before the
hub lock, then again under it before the disarm, so nothing a bar opens in between survives. A
flatten that does not land keeps the row armed and halts the account. Dry runs never flatten.
The armed rows carry no balances, so the hub starts them at once (as in test_hub_orders.py)."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from live_helpers import ARMED_AT, pinned, reconciled

from fenrir.bridge.client import Ack, AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.service import LiveHub
from fenrir.settings import Settings
from fenrir.strategy import file_hash

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26
START = core.Start(50_000.0, 50_000.0, 50_000.0)
OPEN = core.DayStart(50_000.0, 50_000.0)
HELD = {"account": "Sim101", "instrument": "MNQ 12-26", "qty": 1, "avg_price": 20000.0}
BROKER = "no connection to the broker"
NOT_CONFIGURED = "NinjaTrader add-on not configured (FENRIR_NT_TOKEN)"

ALWAYS = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    return state + 1, [Bracket("long", 1, bar.close - 10, bar.close + 20)]
"""


def bar(ts: datetime) -> Bar:
    return Bar(ts, 20000.0, 20005.0, 19995.0, 20000.0, 10, "NQZ26")


def kept(cause: str) -> str:
    return f"disarm failed: {cause}; still armed, halted until disarmed"


def flatten_failed(error: str) -> str:
    return kept(f"flatten failed: {error}; the position may still be open: check NinjaTrader")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")


@pytest.fixture
def conn(settings):
    c = journal.open_db(settings.private_dir / "fenrir.db")
    yield c
    c.close()


@pytest.fixture
def armed(tmp_path, conn) -> Armed:
    path = tmp_path / "always.py"
    path.write_text(ALWAYS)
    a = pinned(
        Armed(
            "Sim101",
            str(path),
            file_hash(path),
            "lucid-flex-50k",
            "MNQ",
            "sim",
            "evaluation",
            ARMED_AT,
        )
    )
    arming.insert_armed(conn, a)
    return a


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture(autouse=True)
def figures(monkeypatch):
    real = core.start

    def sim_start(setup, now, *rest):
        s, out = real(replace(setup, start=START, day_start=OPEN), now, *rest)
        return reconciled(s), out

    monkeypatch.setattr(core, "start", sim_start)


@pytest.fixture
async def fake():
    f = FakeAddon()
    async with running(f) as url:
        f.url = url
        yield f


@pytest.fixture
async def client(fake):
    c = AddonClient(fake.url, fake.token, timeout=0.2, retries=2)
    yield c
    await c.aclose()


@pytest.fixture
async def hub(settings, client):
    h = LiveHub(settings, addon=client)
    yield h
    await h.stop()


def rows(conn, kind=None, account="Sim101"):
    return journal.rows(conn, account=account, kind=kind)


def posts(fake):
    return [path for path, _ in fake.requests]


async def until(check, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("positions", [[HELD], []], ids=["holding", "flat"])
async def test_disarm_flattens_a_sim_account_twice_then_disarms_it(
    hub, conn, armed, fake, client, monkeypatch, positions
):
    """Flat or not, the account's `/flatten` goes out (it also cancels the working orders), once
    before the hub lock and once under it, each under its own id, both while the row is armed."""
    fake.positions = positions
    await hub.tick(T0)
    armed_at_post, flatten = [], client.flatten

    async def spy(client_id, account):
        armed_at_post.append([a.account for a in arming.list_armed(conn)])
        return await flatten(client_id, account)

    monkeypatch.setattr(client, "flatten", spy)
    assert await hub.disarm("Sim101") == {"ok": True, "error": None}
    [(path, body), (again, second)] = fake.requests
    assert path == again == "/flatten" and body["account"] == second["account"] == "Sim101"
    assert body["client_id"] != second["client_id"]
    assert all(len(b["client_id"]) <= 24 for b in (body, second))
    assert armed_at_post == [["Sim101"], ["Sim101"]]  # never after the row is gone
    assert arming.list_armed(conn) == () and hub.states == {} and hub.children == {}
    [halt, alert] = [r for r in rows(conn) if r.kind in ("halt", "alert")]
    assert (halt.kind, halt.payload) == ("halt", service.DISARMED)
    assert alert.payload == {"message": "disarmed; flattened, working orders cancelled"}


async def test_disarm_flattens_a_live_row_that_is_not_running_yet(hub, conn, armed, fake):
    """Armed in the last tick's 5 s (or waiting for NinjaTrader): the mode comes from `armed`."""
    arming.insert_armed(conn, replace(armed, mode="live"))
    assert await hub.disarm("Sim101") == {"ok": True, "error": None}
    assert posts(fake) == ["/flatten", "/flatten"]
    assert arming.list_armed(conn) == ()


@pytest.mark.parametrize("configured", [True, False])
async def test_a_dry_run_disarms_without_flattening(
    settings, conn, armed, fake, client, configured
):
    arming.delete_armed(conn, "Sim101")
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    hub = LiveHub(settings, addon=client if configured else None)
    try:
        await hub.tick(T0)
        assert await hub.disarm("Dry1") == {"ok": True, "error": None}
        assert hub.states == {}
    finally:
        await hub.stop()
    assert fake.requests == [] and arming.list_armed(conn) == ()
    [halt] = rows(conn, account="Dry1")  # the disarm route's row, as before; no alert
    assert (halt.kind, halt.payload) == ("halt", service.DISARMED)


async def test_disarming_an_account_not_armed_is_not_ok_and_sends_nothing(hub, conn, armed, fake):
    assert await hub.disarm("Sim999") == {"ok": False, "error": None}
    assert fake.requests == [] and rows(conn, account="Sim999") == ()


async def test_disarm_before_anything_was_armed_creates_no_database(settings, client, fake):
    hub = LiveHub(settings, addon=client)
    try:
        assert await hub.disarm("Sim101") == {"ok": False, "error": None}
    finally:
        await hub.stop()
    assert not (settings.private_dir / "fenrir.db").exists() and fake.requests == []


async def test_a_position_opened_after_the_disarms_first_flatten_does_not_survive_the_disarm(
    hub, conn, armed, fake, client, monkeypatch
):
    """A bar holds the hub lock while its Bracket is out; the disarm flattens at once, then the
    Bracket lands and opens a position. The flatten under the lock closes it, and nothing opens
    one after it: the row is gone and the runner stopped before the lock is released."""
    book, posting, go = [], asyncio.Event(), asyncio.Event()
    bracket, flatten = client.bracket, client.flatten

    async def slow_bracket(cid, account, *rest):
        posting.set()
        await go.wait()
        ack = await bracket(cid, account, *rest)
        book.append(account)  # its entry filled: a position NinjaTrader holds
        return ack

    async def flattening(client_id, account):
        ack = await flatten(client_id, account)
        book.clear()
        return ack

    monkeypatch.setattr(client, "bracket", slow_bracket)
    monkeypatch.setattr(client, "flatten", flattening)
    await hub.tick(T0)
    on_bar = asyncio.create_task(hub.handle_bar(bar(T0)))
    await asyncio.wait_for(posting.wait(), 10)
    disarm = asyncio.create_task(hub.disarm("Sim101"))
    try:
        await until(lambda: fake.requests, timeout=2.0)  # flattened while the bar holds the lock
        assert posts(fake) == ["/flatten"] and not disarm.done()
    finally:
        go.set()
    await on_bar
    assert await disarm == {"ok": True, "error": None}
    assert book == []  # the position the late Bracket opened did not survive the disarm
    assert posts(fake) == ["/flatten", "/bracket", "/flatten"]
    await hub.handle_bar(bar(T0 + timedelta(minutes=1)))
    assert posts(fake) == ["/flatten", "/bracket", "/flatten"] and book == []


async def test_the_flatten_under_the_lock_decides(hub, conn, armed, fake):
    """The first flatten refused, the second landed: the account is flat and disarmed."""
    await hub.tick(T0)
    fake.rejects = [BROKER]
    assert await hub.disarm("Sim101") == {"ok": True, "error": None}
    assert arming.list_armed(conn) == ()


async def test_a_duplicate_flatten_counts_as_landed(hub, conn, armed, client, monkeypatch):
    async def duplicate(client_id, account):
        return Ack(False, True, None)

    monkeypatch.setattr(client, "flatten", duplicate)
    assert await hub.disarm("Sim101") == {"ok": True, "error": None}
    assert arming.list_armed(conn) == ()


async def _unreachable(client_id, account):
    raise httpx.ConnectError("add-on unreachable")


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("refused", BROKER),
        ("refused, no reason", "refused"),
        ("unreachable", "ConnectError: add-on unreachable"),
        ("not configured", NOT_CONFIGURED),
    ],
)
async def test_a_flatten_that_does_not_land_keeps_the_row_and_halts_the_account(
    settings, conn, armed, fake, client, monkeypatch, failure, error
):
    """Fail closed: the account stays armed (its runner still marks, breaches and flattens at
    flat-by) but halted, journalled so a restart keeps it so, with an alert, and the answer says
    the position may still be open."""
    if failure == "refused":
        fake.rejects = [BROKER, BROKER]
    elif failure == "refused, no reason":
        monkeypatch.setattr(client, "flatten", lambda *a: _ack_refused())
    elif failure == "unreachable":
        monkeypatch.setattr(client, "flatten", _unreachable)
    hub = LiveHub(settings, addon=None if failure == "not configured" else client)
    try:
        await hub.tick(T0)
        result = await hub.disarm("Sim101")
        why = flatten_failed(error)
        assert result == {"ok": False, "error": why}
        assert [a.account for a in arming.list_armed(conn)] == ["Sim101"]
        assert hub.states["Sim101"].halted == why
        assert [r.payload for r in rows(conn, "halt")] == [
            {"reason": why, "kind": None, "until": None}
        ]
        assert [r.payload for r in rows(conn, "alert")] == [{"message": why}]
        before = len(fake.requests)
        await hub.handle_bar(bar(T0))  # halted: no entry
        assert len(fake.requests) == before
    finally:
        await hub.stop()
    restarted = LiveHub(settings, addon=client)
    try:
        await restarted.tick(T0 + timedelta(seconds=5))
        assert restarted.states["Sim101"].halted == why
    finally:
        await restarted.stop()


async def _ack_refused():
    return Ack(False, False, None)


async def test_a_failed_flatten_halts_a_row_that_is_not_running_yet(
    settings, conn, armed, client, monkeypatch
):
    """Not started yet (armed in the last 5 s, or waiting for NinjaTrader): its halt row is
    journalled, so it starts halted."""
    monkeypatch.setattr(client, "flatten", _unreachable)
    hub = LiveHub(settings, addon=client)
    try:
        why = flatten_failed("ConnectError: add-on unreachable")
        assert await hub.disarm("Sim101") == {"ok": False, "error": why}
        assert [r.payload for r in rows(conn, "alert")] == [{"message": why}]
        await hub.tick(T0)
        assert hub.states["Sim101"].halted == why
    finally:
        await hub.stop()


async def test_a_disarm_sqlite_refuses_keeps_the_row_and_halts_the_flattened_account(
    hub, conn, armed, fake, monkeypatch
):
    """Flattened, but the halt row and delete could not be written (e.g. the arm route holds the
    write lock): still armed, so halted in memory, and nothing new goes out."""
    await hub.tick(T0)

    def locked(conn, account, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(arming, "disarm", locked)
    why = kept("OperationalError: database is locked")
    assert await hub.disarm("Sim101") == {"ok": False, "error": why}
    assert posts(fake) == ["/flatten", "/flatten"]
    assert [a.account for a in arming.list_armed(conn)] == ["Sim101"]
    assert hub.states["Sim101"].halted == why
    await hub.handle_bar(bar(T0))
    assert posts(fake) == ["/flatten", "/flatten"]


async def test_disarm_names_a_failed_runner_stop_as_such(hub, conn, armed, fake, monkeypatch):
    """Flattened and the row deleted, only stopping the runner failed: halted until the next
    sync stops it."""
    await hub.tick(T0)

    async def broken(now):
        raise RuntimeError("cannot stop")

    monkeypatch.setattr(hub, "_sync", broken)
    why = "disarmed, but stopping the runner failed: RuntimeError: cannot stop"
    assert await hub.disarm("Sim101") == {"ok": False, "error": why}
    assert arming.list_armed(conn) == ()
    assert hub.states["Sim101"].halted == why
