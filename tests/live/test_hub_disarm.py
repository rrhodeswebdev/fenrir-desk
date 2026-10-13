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
from live_helpers import ARMED_AT, pinned, reconciled, stored

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
NOT_JOURNALLED = "halt held in memory, not journalled (a restart before it starts lifts it)"
NOT_JOURNALLED_YET = (
    "halt held in memory, not journalled yet (retried each tick; a restart before then lifts it)"
)
LOCKED = "OperationalError: database is locked"

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


def account(account_id: str = "Sim101") -> dict:
    """An add-on "account" stream message (absolute $), which starts a waiting sim row."""
    return {
        "type": "account",
        "id": account_id,
        "name": account_id,
        "is_sim": True,
        "balance": 50_300.0,
        "realized_pnl_today": 0.0,
        "unrealized_pnl": 0.0,
    }


def _locked(*args):
    raise sqlite3.OperationalError("database is locked")


def kept(cause: str) -> str:
    return f"disarm failed: {cause}; still armed, halted until disarmed"


def flatten_failed(error: str) -> str:
    return kept(f"flatten failed: {error}; the position may still be open: check NinjaTrader")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")
    stored(s.data_dir)  # warm-up (#79) needs the store up to the last trading day
    return s


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
        assert [r.payload for r in rows(conn, "halt")] == [
            {"reason": why, "kind": None, "until": None}  # until disarmed, not a day halt
        ]
        assert [r.payload for r in rows(conn, "alert")] == [{"message": why}]
        await hub.tick(T0)
        assert hub.states["Sim101"].halted == why
    finally:
        await hub.stop()


async def test_a_double_fault_still_starts_a_row_that_is_not_running_yet_halted(
    settings, conn, armed, client, monkeypatch, clock
):
    """The flatten failed and sqlite refused the halt row too (e.g. locked): the halt is held in
    memory, as the kill's `killed`, so the row still starts halted (journalled then), and the
    answer says the halt was not journalled. It halts only that arming: a re-arm starts free."""
    write = journal.write

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client, "flatten", _unreachable)
    monkeypatch.setattr(journal, "write", locked)
    hub = LiveHub(settings, addon=client)
    try:
        why = flatten_failed("ConnectError: add-on unreachable")
        assert await hub.disarm("Sim101") == {
            "ok": False,
            "error": f"{why}; {NOT_JOURNALLED}: OperationalError: database is locked",
        }
        assert rows(conn, "halt") == ()
        monkeypatch.setattr(journal, "write", write)
        await hub.tick(T0)
        assert hub.states["Sim101"].halted == why
        assert [r.payload for r in rows(conn, "halt")] == [
            {"reason": why, "kind": None, "until": None}
        ]
        assert (await hub.kill())["ok"]  # disarmed by the kill, which leaves `held` as it is
        clock["t"] = T0 + timedelta(minutes=1)
        arming.insert_armed(conn, replace(armed, armed_at=clock["t"]))
        await hub.tick(clock["t"])
        assert hub.states["Sim101"].halted is None
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


async def test_a_failed_read_under_the_lock_says_the_position_may_still_be_open(
    hub, conn, armed, fake, monkeypatch
):
    """`armed` unreadable (both reads, so nothing was flattened): kept and halted, and the answer
    warns that the position may still be open, never that the flatten landed."""
    await hub.tick(T0)
    monkeypatch.setattr(service, "list_armed", _locked)
    why = kept(
        f"cannot read the armed accounts: {LOCKED}; not flattened under the lock: the position"
        " may still be open: check NinjaTrader"
    )
    assert await hub.disarm("Sim101") == {"ok": False, "error": why}
    assert posts(fake) == []
    assert hub.states["Sim101"].halted == why


async def test_a_running_rows_double_fault_says_its_halt_row_is_not_journalled_yet(
    hub, conn, armed, client, monkeypatch
):
    """Halted in memory and its halt row queued: the answer says it is not journalled yet, and
    the next tick writes it."""
    await hub.tick(T0)
    monkeypatch.setattr(client, "flatten", _unreachable)
    write = journal.write
    monkeypatch.setattr(journal, "write", _locked)
    why = flatten_failed("ConnectError: add-on unreachable")
    assert await hub.disarm("Sim101") == {
        "ok": False,
        "error": f"{why}; {NOT_JOURNALLED_YET}: {LOCKED}",
    }
    assert hub.states["Sim101"].halted == why
    monkeypatch.setattr(journal, "write", write)
    await hub.tick(T0 + timedelta(seconds=5))
    assert [r.payload for r in rows(conn, "halt")] == [{"reason": why, "kind": None, "until": None}]


@pytest.mark.parametrize("refused", [False, True], ids=["halt journalled", "double fault"])
async def test_a_kept_row_waiting_for_ninjatrader_shows_the_reason_and_loads_nothing(
    settings, conn, armed, client, monkeypatch, refused
):
    """A sim row waiting for NinjaTrader's account message, whose flatten failed (and, in the
    double fault, whose halt row sqlite refused): `held` shows the disarm reason while it waits,
    and on the message it starts halted without loading its strategy, as the kill's `killed`.
    Armed at the marker's own time, it is still held. Another account starts free."""
    balances = {"start_balance": 50_000.0, "peak_balance": 50_500.0}
    arming.delete_armed(conn, "Sim101")
    arming.insert_armed(conn, replace(armed, armed_at=T0, **balances))
    arming.insert_armed(conn, replace(armed, account="Sim102", **balances))
    monkeypatch.setattr(client, "flatten", _unreachable)
    hub = LiveHub(settings, addon=client)
    try:
        await hub.tick(T0)
        assert hub.states == {} and set(hub.waiting) == {"Sim101", "Sim102"}
        why = flatten_failed("ConnectError: add-on unreachable")
        write = journal.write
        if refused:
            monkeypatch.setattr(journal, "write", _locked)
        error = f"{why}; {NOT_JOURNALLED}: {LOCKED}" if refused else why
        assert await hub.disarm("Sim101") == {"ok": False, "error": error}
        monkeypatch.setattr(journal, "write", write)
        reasons = {a["account"]: a["halt_reason"] for a in hub.status()["accounts"]}
        assert reasons["Sim101"] == why
        assert reasons["Sim102"].startswith("waiting for NinjaTrader")
        await hub.handle_stream(account("Sim101"))
        assert hub.states["Sim101"].halted == why and hub.children == {}
        [acc] = [a for a in hub.status()["accounts"] if a["account"] == "Sim101"]
        assert acc["conditions"] == [{"kind": None, "reason": why}]  # no strategy: not warming
        assert [r.payload for r in rows(conn, "halt")] == [
            {"reason": why, "kind": None, "until": None}
        ]
        await hub.handle_stream(account("Sim102"))
        assert hub.states["Sim102"].halted is None and set(hub.children) == {"Sim102"}
    finally:
        await hub.stop()


async def test_a_second_double_fault_on_a_running_row_still_says_not_journalled_yet(
    hub, conn, armed, client, monkeypatch
):
    """#230: a second double fault with another error. The account is already halted by the
    first (so no new halt row), whose halt row is still queued: the answer still says the halt
    is not journalled yet, and the next tick writes the first one."""
    await hub.tick(T0)
    write = journal.write
    monkeypatch.setattr(journal, "write", _locked)
    monkeypatch.setattr(client, "flatten", _unreachable)
    first = flatten_failed("ConnectError: add-on unreachable")
    assert (await hub.disarm("Sim101"))["error"] == f"{first}; {NOT_JOURNALLED_YET}: {LOCKED}"
    monkeypatch.setattr(client, "flatten", lambda *a: _ack_refused())
    second = flatten_failed("refused")
    assert await hub.disarm("Sim101") == {
        "ok": False,
        "error": f"{second}; {NOT_JOURNALLED_YET}: {LOCKED}",
    }
    assert hub.states["Sim101"].halted == first
    monkeypatch.setattr(journal, "write", write)
    await hub.tick(T0 + timedelta(seconds=5))
    assert [r.payload for r in rows(conn, "halt")] == [
        {"reason": first, "kind": None, "until": None}
    ]


async def test_a_landed_disarm_clears_only_its_own_accounts_held_halt(
    settings, conn, armed, client, monkeypatch
):
    """#230 (mutant Y9): Sim101 waits for NinjaTrader, double-faulted (its halt only `held`);
    a disarm of Sim102 that lands leaves Sim101's held halt alone, so Sim101 still starts halted
    on its account message, loading nothing."""
    balances = {"start_balance": 50_000.0, "peak_balance": 50_500.0}
    arming.delete_armed(conn, "Sim101")
    arming.insert_armed(conn, replace(armed, **balances))
    arming.insert_armed(conn, replace(armed, account="Sim102", **balances))
    hub = LiveHub(settings, addon=client)
    try:
        await hub.tick(T0)
        assert set(hub.waiting) == {"Sim101", "Sim102"}
        flatten, write = client.flatten, journal.write
        monkeypatch.setattr(client, "flatten", _unreachable)
        monkeypatch.setattr(journal, "write", _locked)
        why = flatten_failed("ConnectError: add-on unreachable")
        assert await hub.disarm("Sim101") == {
            "ok": False,
            "error": f"{why}; {NOT_JOURNALLED}: {LOCKED}",
        }
        monkeypatch.setattr(client, "flatten", flatten)
        monkeypatch.setattr(journal, "write", write)
        assert await hub.disarm("Sim102") == {"ok": True, "error": None}
        assert set(hub.held) == {"Sim101"}
        await hub.handle_stream(account("Sim101"))
        assert hub.states["Sim101"].halted == why and hub.children == {}
    finally:
        await hub.stop()


# #230: disarm without flatten, only for an account the add-on no longer manages

UNMANAGED = "unmanaged_account"  # the add-on's refusal for an account not in managed_accounts
SKIPPED = (
    "disarmed without its flatten: the NinjaTrader add-on no longer manages this account"
    " (unmanaged_account); any position or working order there must be closed in NinjaTrader"
)


async def test_an_unmanaged_accounts_refused_flatten_keeps_it_and_says_it_is_unmanaged(
    hub, conn, armed, fake
):
    """A plain disarm still fails closed; the answer adds `unmanaged`, so the dialog can offer
    the confirmed disarm without flatten."""
    await hub.tick(T0)
    fake.rejects = [UNMANAGED, UNMANAGED]
    why = flatten_failed(UNMANAGED)
    assert await hub.disarm("Sim101") == {"ok": False, "error": why, "unmanaged": True}
    assert [a.account for a in arming.list_armed(conn)] == ["Sim101"]
    assert hub.states["Sim101"].halted == why


@pytest.mark.parametrize("running", [True, False], ids=["running", "not running yet"])
async def test_disarm_without_flatten_disarms_an_account_the_addon_no_longer_manages(
    hub, conn, armed, fake, running
):
    """Refused as unmanaged and kept, then confirmed without flatten: both flattens are still
    posted, and refused as unmanaged, so the row is disarmed (its halt row and delete) with an
    alert naming the skipped flatten; the runner is stopped and any held halt dropped."""
    if running:
        await hub.tick(T0)
    fake.rejects = [UNMANAGED, UNMANAGED]
    kept_why = flatten_failed(UNMANAGED)
    assert (await hub.disarm("Sim101"))["unmanaged"] is True
    fake.rejects = [UNMANAGED, UNMANAGED]
    assert await hub.disarm("Sim101", flatten=False) == {"ok": True, "error": None}
    assert posts(fake) == ["/flatten"] * 4
    assert arming.list_armed(conn) == () and hub.states == {} and hub.children == {}
    assert hub.held == {}
    assert [r.payload for r in rows(conn, "halt")] == [
        {"reason": kept_why, "kind": None, "until": None},
        service.DISARMED,
    ]
    assert [r.payload for r in rows(conn, "alert")][-1] == {"message": SKIPPED}


@pytest.mark.parametrize("mode", ["sim", "live"])
@pytest.mark.parametrize("rejects", [[], [UNMANAGED]], ids=["both land", "second lands"])
async def test_disarm_without_flatten_still_flattens_a_managed_account(
    hub, conn, armed, fake, mode, rejects
):
    """The flag never skips a flatten that lands: a managed account is flattened, under the
    lock too, and disarmed as by a plain disarm."""
    arming.insert_armed(conn, replace(armed, mode=mode))
    if mode == "sim":
        await hub.tick(T0)
    fake.rejects = list(rejects)
    assert await hub.disarm("Sim101", flatten=False) == {"ok": True, "error": None}
    assert posts(fake) == ["/flatten", "/flatten"] and arming.list_armed(conn) == ()
    assert [r.payload for r in rows(conn, "alert")] == [
        {"message": "disarmed; flattened, working orders cancelled"}
    ]


async def _unreachable_unmanaged(client_id, account):
    raise httpx.ConnectError(UNMANAGED)


async def _ack_unmanaged_landed():
    return Ack(True, False, UNMANAGED)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("refused", BROKER),
        ("unknown account", "unknown account"),
        ("refused, no reason", "refused"),
        ("unreachable", "ConnectError: add-on unreachable"),
        ("unreachable, unmanaged text", f"ConnectError: {UNMANAGED}"),
        ("not configured", NOT_CONFIGURED),
    ],
)
async def test_disarm_without_flatten_fails_closed_on_any_other_failure(
    settings, conn, armed, fake, client, monkeypatch, failure, error
):
    """Only the add-on's own `unmanaged_account` refusal lets the flatten be skipped: any other
    refusal or failure (an error text that merely says it included) keeps the row and halts the
    account, as a plain disarm does, with no `unmanaged` in the answer."""
    if failure in ("refused", "unknown account"):
        fake.rejects = [error, error]
    elif failure == "refused, no reason":
        monkeypatch.setattr(client, "flatten", lambda *a: _ack_refused())
    elif failure == "unreachable":
        monkeypatch.setattr(client, "flatten", _unreachable)
    elif failure == "unreachable, unmanaged text":
        monkeypatch.setattr(client, "flatten", _unreachable_unmanaged)
    hub = LiveHub(settings, addon=None if failure == "not configured" else client)
    try:
        await hub.tick(T0)
        why = flatten_failed(error)
        assert await hub.disarm("Sim101", flatten=False) == {"ok": False, "error": why}
        assert [a.account for a in arming.list_armed(conn)] == ["Sim101"]
        assert hub.states["Sim101"].halted == why
        assert service.DISARMED not in [r.payload for r in rows(conn, "halt")]
    finally:
        await hub.stop()


async def test_an_accepted_flatten_is_never_read_as_unmanaged(
    hub, conn, armed, client, monkeypatch
):
    """`accepted` with an `unmanaged_account` error text landed: a plain flattened disarm."""
    monkeypatch.setattr(client, "flatten", lambda *a: _ack_unmanaged_landed())
    assert await hub.disarm("Sim101", flatten=False) == {"ok": True, "error": None}
    assert [r.payload for r in rows(conn, "alert")] == [
        {"message": "disarmed; flattened, working orders cancelled"}
    ]


async def test_a_dry_run_disarm_without_flatten_is_a_plain_dry_disarm(
    settings, conn, armed, fake, client
):
    arming.delete_armed(conn, "Sim101")
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    hub = LiveHub(settings, addon=client)
    try:
        await hub.tick(T0)
        assert await hub.disarm("Dry1", flatten=False) == {"ok": True, "error": None}
    finally:
        await hub.stop()
    assert fake.requests == [] and arming.list_armed(conn) == ()
    assert [r.kind for r in rows(conn, account="Dry1")] == ["halt"]
