"""22.4: the hub posts the core's Sends to the add-on (the fake), journals failed sends, replays
journalled fills on a restart. 22.5: it follows the add-on's event stream (reconcile, lost link,
account figures, fills), resolves and kills. The hub cannot start sim yet (23.3 stores the start
figures), so `figures` supplies them, and reconciles with a flat book as on `connected`."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from live_helpers import ARMED_AT, fill, pinned, reconciled

from fenrir.bridge.client import Ack, AddonClient
from fenrir.bridge.fake_addon import FakeAddon, now_ts, running
from fenrir.core.actions import Bracket, Flatten
from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.service import LiveHub
from fenrir.server.app import create_app
from fenrir.settings import Settings
from fenrir.strategy import Position, file_hash

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26
MNQ = "MNQ 12-26"  # NinjaTrader's name for it
START = core.Start(50_000.0, 50_000.0, 50_000.0)
REAL_START = core.start  # before `figures` patches it
OPEN = core.DayStart(50_000.0, 50_000.0)

ALWAYS = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    return state + 1, [Bracket("long", 1, bar.close - 10, bar.close + 20)]
"""
BRACKET = Bracket("long", 1, 19990.0, 20020.0)  # what ALWAYS returns on bar(T0)
CID = core.client_id("Sim101", T0, "0", BRACKET)
BODY = {
    "client_id": CID,
    "account": "Sim101",
    "instrument": MNQ,
    "side": "long",
    "qty": 1,
    "stop": 19990.0,
    "target": 20020.0,
}


def bar(ts: datetime) -> Bar:
    return Bar(ts, 20000.0, 20005.0, 19995.0, 20000.0, 10, "NQZ26")


def mnq_fill(qty, price, ts, cid="other", **kw):
    return {**fill(qty, price, ts, cid=cid), "instrument": MNQ, **kw}


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


def rows(conn, kind=None):
    return journal.rows(conn, account="Sim101", kind=kind)


def kinds(conn):
    return [r.kind for r in rows(conn)]


def locked(monkeypatch, *refused):
    """sqlite refuses writes of these kinds (all when none) until the returned switch is off."""
    write, on = journal.write, {"on": True}

    def flaky(conn, ts, account, kind, payload):
        if on["on"] and (not refused or kind in refused):
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    monkeypatch.setattr(journal, "write", flaky)
    return on


async def test_a_bracket_reaches_the_addon_with_the_contract_body(hub, conn, armed, fake):
    await hub.handle_bar(bar(T0))
    assert fake.requests == [("/bracket", BODY)]
    [action] = rows(conn, "action")
    assert (action.payload["sent"], action.payload["client_id"]) == (True, CID)
    assert hub.states["Sim101"].working.client_id == CID


async def test_the_working_entry_is_kept_before_the_post(hub, armed, client, monkeypatch):
    """A fast entry fill must find its working entry, or it counts as foreign and is flattened."""
    seen, post = [], client.bracket

    async def spy(*args):
        seen.append(hub.states["Sim101"].working)
        return await post(*args)

    monkeypatch.setattr(client, "bracket", spy)
    await hub.handle_bar(bar(T0))
    assert [w.client_id for w in seen] == [CID]


async def test_a_refused_send_is_journalled_as_alert_and_frees_the_entry(hub, conn, armed, fake):
    fake.rejects = ["insufficient margin"]
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action", "alert"]
    [alert] = rows(conn, "alert")
    assert alert.payload == {"message": f"send {CID} failed: insufficient margin"}
    assert hub.states["Sim101"].working is None


async def test_rows_after_a_refused_send_are_still_written(hub, conn, armed, fake, monkeypatch):
    decide, peak = core.decide, {"peak": 50_100.0, "ts": T0.isoformat()}

    def then_peak(*args):
        s, out = decide(*args)
        return s, (*out, core.Log("peak", peak))

    monkeypatch.setattr(core, "decide", then_peak)
    fake.rejects = ["no"]
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action", "alert", "peak"]


async def test_a_timeout_is_not_a_refusal(hub, conn, armed, fake):
    """The order may have landed: the entry stays working (no second bracket) until a fill, the
    day roll or reconciliation."""
    fake.delays = [0.5, 0.5]  # past the client's 0.2 s, on both attempts
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/bracket", "/bracket"]
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} outcome unknown: ")
    assert hub.states["Sim101"].working.client_id == CID


async def test_a_duplicate_with_nothing_at_ninjatrader_frees_the_entry(hub, conn, armed, fake):
    """The first attempt was refused but its reply lost: the retry's `duplicate` hides that."""
    fake.rejects, fake.delays = ["rejected"], [0.5]
    await hub.handle_bar(bar(T0))
    assert fake.requests == [("/bracket", BODY), ("/bracket", BODY)]
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} failed: duplicate, but NinjaTrader")
    assert hub.states["Sim101"].working is None


ENTRY = {
    "account": "Sim101",
    "order_id": "1",
    "client_id": CID,
    "instrument": MNQ,
    "role": "entry",
    "action": "buy",
    "qty": 1,
    "price": 20000.0,
    "state": "Working",
}
HELD = {"account": "Sim101", "instrument": MNQ, "qty": 1, "avg_price": 20000.0}


@pytest.mark.parametrize(("orders", "positions"), [([ENTRY], []), ([], [HELD])])
async def test_a_duplicate_that_landed_stays_working(hub, conn, armed, fake, orders, positions):
    fake.seen.add(CID)  # an earlier attempt (or process) already sent it
    fake.orders, fake.positions = orders, positions
    await hub.handle_bar(bar(T0))
    assert "alert" not in kinds(conn)
    assert hub.states["Sim101"].working.client_id == CID


@pytest.mark.parametrize(
    ("orders", "positions"),
    [
        ([{**ENTRY, "role": "stop"}], []),  # not its entry
        ([{**ENTRY, "client_id": "other"}], []),
        ([], [{**HELD, "instrument": "MES 12-26"}]),  # a position on another instrument
        ([], [{**HELD, "qty": 0}]),
    ],
)
async def test_a_duplicate_is_freed_unless_its_entry_or_a_position_is_there(
    hub, conn, armed, fake, orders, positions
):
    fake.seen.add(CID)
    fake.orders, fake.positions = orders, positions
    await hub.handle_bar(bar(T0))
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} failed: duplicate, but NinjaTrader")
    assert hub.states["Sim101"].working is None


@pytest.mark.parametrize("failing", ["orders", "positions"])
async def test_a_duplicate_that_cannot_be_checked_stays_working(
    hub, conn, armed, fake, client, monkeypatch, failing
):
    """Unknown counts as landed: freeing it could let the strategy double the entry."""

    async def down(account):
        raise httpx.ConnectError("add-on unreachable")

    monkeypatch.setattr(client, failing, down)
    fake.seen.add(CID)
    await hub.handle_bar(bar(T0))
    assert "alert" not in kinds(conn)
    assert hub.states["Sim101"].working.client_id == CID


async def test_no_addon_configured_is_a_failed_send(settings, conn, armed):
    hub = LiveHub(settings)
    try:
        await hub.handle_bar(bar(T0))
    finally:
        await hub.stop()
    [alert] = rows(conn, "alert")
    assert "not configured" in alert.payload["message"]
    assert hub.states["Sim101"].working is None


FOREIGN_TS = T0 + timedelta(seconds=30)
FOREIGN = mnq_fill(1, 20000.0, FOREIGN_TS)  # no bracket of ours: no known stop, flattened at once
FLATTEN = {"client_id": core.client_id("Sim101", FOREIGN_TS, "f1", Flatten()), "account": "Sim101"}


async def test_a_timed_out_flatten_is_retried_under_the_same_client_id(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill(FOREIGN)
    assert fake.requests == [("/flatten", FLATTEN)] * 2
    assert hub.states["Sim101"].exiting == FLATTEN["client_id"]
    await hub.tick(T0 + timedelta(seconds=5))  # answered `duplicate`: the first one landed
    await hub.tick(T0 + timedelta(seconds=10))
    assert fake.requests == [("/flatten", FLATTEN)] * 3
    alerts = [r.payload["message"] for r in rows(conn, "alert")]
    assert [m.split(":")[0] for m in alerts] == [
        "flattening",
        f"send {FLATTEN['client_id']} outcome unknown",
    ]


async def test_a_flatten_is_resent_until_it_is_answered(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5] * 4  # the first post and the first resend both time out
    await hub.handle_fill(FOREIGN)
    await hub.tick(T0 + timedelta(seconds=5))
    assert hub.retry["Sim101"].client_id == FLATTEN["client_id"]
    await hub.tick(T0 + timedelta(seconds=10))
    assert fake.requests == [("/flatten", FLATTEN)] * 5
    assert len(rows(conn, "alert")) == 2  # "flattening" and one "outcome unknown"
    assert "Sim101" not in hub.retry


async def test_a_flatten_is_not_retried_once_flat(hub, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill(FOREIGN)
    await hub.handle_fill(mnq_fill(-1, 20001.0, T0 + timedelta(seconds=40)))
    await hub.tick(T0 + timedelta(seconds=5))
    assert len(fake.requests) == 2
    assert hub.states["Sim101"].exiting is None


async def test_only_this_accounts_instrument_reaches_on_fill(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_fill({**FOREIGN, "instrument": "MES 12-26"})
    await hub.handle_fill({**FOREIGN, "account": "Sim102"})
    assert kinds(conn) == []
    assert hub.states["Sim101"].position.qty == 0


async def test_dry_runs_take_no_fills(hub, conn, armed):
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    await hub.tick(T0)
    await hub.handle_fill({**FOREIGN, "account": "Dry1"})
    assert [r.kind for r in journal.rows(conn, account="Dry1")] == []
    assert hub.states["Dry1"].position.qty == 0


async def test_only_the_entry_fill_sets_drift_ticks(hub, armed):
    await hub.handle_bar(bar(T0))  # a working entry, then a fill that is not its own
    await hub.handle_fill(mnq_fill(1, 20010.0, T0 + timedelta(minutes=1, seconds=1)))
    assert hub.drift_ticks == {}


async def test_disarming_drops_the_flatten_retry_and_drift_ticks(hub, conn, armed):
    await hub.tick(T0)
    hub.retry["Sim101"] = core.Send(Flatten(), FLATTEN["client_id"])
    hub.drift_ticks["Sim101"] = 1.0
    arming.delete_armed(conn, "Sim101")
    await hub.tick(T0 + timedelta(seconds=5))
    assert (hub.retry, hub.drift_ticks) == ({}, {})


async def test_one_accounts_refused_rows_never_hold_back_anothers_flatten(
    hub, conn, armed, fake, monkeypatch
):
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill({**FOREIGN, "account": "Sim102"})
    write = journal.write

    def locked_101(conn, ts, account, kind, payload):
        if account == "Sim101":
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    monkeypatch.setattr(journal, "write", locked_101)
    hub.unwritten["Sim101"] = (core.Log("peak", {"peak": 50_100.0, "ts": T0.isoformat()}),)
    await hub.tick(T0 + timedelta(seconds=5))  # Sim101 ticks first and fails
    assert [path for path, _ in fake.requests] == ["/flatten"] * 3
    assert "Sim101" in hub.unwritten


async def test_status_reports_the_entry_fill_drift_in_ticks(hub, armed):
    await hub.handle_bar(bar(T0))
    assert hub.status()["drift"] == {"ok": True, "ticks": None}
    await hub.handle_fill(mnq_fill(1, 20001.25, T0 + timedelta(minutes=1, seconds=1), cid=CID))
    assert hub.status()["drift"] == {"ok": False, "ticks": 5.0}  # 1.25 points of 0.25


async def test_a_restart_replays_journalled_fills(settings, hub, client, armed):
    await hub.handle_bar(bar(T0))
    await hub.handle_fill(mnq_fill(1, 20000.0, T0 + timedelta(minutes=1, seconds=1), cid=CID))
    await hub.stop()
    restarted = LiveHub(settings, addon=client)
    try:
        await restarted.tick(T0 + timedelta(minutes=2))
        s = restarted.states["Sim101"]
    finally:
        await restarted.stop()
    assert s.position == Position(1, 20000.0, 19990.0, 20020.0)
    assert s.working is None and len(s.fills) == 1


async def test_a_restart_replays_only_todays_rows(settings, hub, client, conn, armed):
    """Rows since arming but from an earlier trading day are not today's book."""
    arming.insert_armed(conn, replace(armed, armed_at=T0 - timedelta(days=1, hours=1)))
    yesterday = T0 - timedelta(days=1)
    journal.write(conn, yesterday, "Sim101", "fill", mnq_fill(1, 20000.0, yesterday))
    await hub.tick(T0)
    assert hub.states["Sim101"].position.qty == 0


async def test_a_bracket_whose_row_was_refused_is_not_sent(hub, armed, fake, monkeypatch):
    locked(monkeypatch, "action")
    await hub.handle_bar(bar(T0))  # the hub halts the account on the journal error
    assert fake.requests == []
    assert hub.states["Sim101"].working is None


async def test_a_flatten_goes_out_while_the_journal_refuses(hub, armed, fake, monkeypatch):
    await hub.tick(T0)
    locked(monkeypatch, "fill")
    with pytest.raises(sqlite3.OperationalError):
        await hub.handle_fill(FOREIGN)
    assert fake.requests == [("/flatten", FLATTEN)]


async def test_refused_peak_rows_and_every_restored_row_after_them_are_retried(
    hub, conn, armed, monkeypatch
):
    """Rows after the first refused one are not tried; the restored kinds among them are kept."""
    on_bar = core.on_bar
    peak = {"peak": 50_100.0, "ts": T0.isoformat()}
    breach = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}

    def fake_on_bar(s, b):
        s, out = on_bar(s, b)
        s = replace(s, halted=breach["reason"], halt_kind="breach")
        return s, (*out, core.Log("peak", peak), core.Log("halt", breach))

    monkeypatch.setattr(core, "on_bar", fake_on_bar)
    await hub.tick(T0)
    switch = locked(monkeypatch, "peak")
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == []  # the halt after the refused peak is not tried yet
    switch["on"] = False
    await hub.tick(T0 + timedelta(seconds=5))
    assert [r.payload for r in rows(conn) if r.kind in ("peak", "halt")][:2] == [peak, breach]


async def test_stop_tries_once_to_flush_queued_rows(hub, conn, armed, monkeypatch):
    breach = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}
    on_bar = core.on_bar

    def breaching(s, b):
        s, out = on_bar(s, b)
        return replace(s, halted=breach["reason"], halt_kind="breach"), (
            *out,
            core.Log("halt", breach),
        )

    monkeypatch.setattr(core, "on_bar", breaching)
    await hub.tick(T0)
    switch = locked(monkeypatch)
    await hub.handle_bar(bar(T0))
    switch["on"] = False
    await hub.stop()
    assert breach in [r.payload for r in rows(conn, "halt")]


def test_create_app_sets_the_addon_client(tmp_path):
    settings = Settings(private_dir=tmp_path / "private", nt_token="t")
    app = create_app(settings, "k" * 43, "c" * 22, None)
    assert isinstance(app.state.addon, AddonClient)
    assert app.state.live.addon is app.state.addon
    with TestClient(app, base_url="http://localhost:8000"):
        pass
    assert app.state.addon._http.is_closed
    unset = create_app(replace(settings, nt_token=""), "k" * 43, "c" * 22, None)
    assert unset.state.addon is None and unset.state.live.addon is None


# 22.5: the add-on's event stream, resolve and the kill switch


def unreconciled(monkeypatch):
    """Sim starts as the hub will start it: paused until NinjaTrader's book is read."""

    def sim_start(setup, now, *rest):
        return REAL_START(replace(setup, start=START, day_start=OPEN), now, *rest)

    monkeypatch.setattr(core, "start", sim_start)


ACCOUNT = {
    "type": "account",
    "id": "Sim101",
    "name": "Sim101",
    "is_sim": True,
    "balance": 50_100.0,
    "realized_pnl_today": 100.0,
    "unrealized_pnl": 0.0,
}
STRAY = {**ENTRY, "client_id": "manual", "role": "other"}
LOST = {"type": "disconnected", "error": "no heartbeat or open timed out"}


async def until(check, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


async def test_connected_reconciles_and_unpauses(hub, conn, armed, fake, monkeypatch):
    unreconciled(monkeypatch)
    await hub.tick(T0)  # not linked yet: nothing to reconcile with
    assert hub.states["Sim101"].unreconciled == core.UNRECONCILED
    await hub.handle_stream({"type": "connected"})
    assert hub.states["Sim101"].unreconciled is None
    assert hub.status()["nt"] == {"connected": True, "last_heartbeat": None}


async def test_disconnected_pauses_until_reconnected(hub, conn, armed, fake):
    await hub.handle_stream({"type": "connected"})
    await hub.tick(T0)
    await hub.handle_stream(LOST)
    assert hub.states["Sim101"].unreconciled == core.LINK_LOST
    assert hub.status()["nt"]["connected"] is False
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(core.LINK_LOST)
    await hub.handle_bar(bar(T0))
    assert fake.requests == []  # no new entries while the link is down
    await hub.handle_stream({"type": "connected"})
    assert hub.states["Sim101"].unreconciled is None


async def test_a_mismatch_halts_until_resolved(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.orders = [STRAY]
    await hub.handle_stream({"type": "connected"})
    assert "stray_orders" in hub.states["Sim101"].mismatch
    assert hub.status()["accounts"][0]["halt_kind"] == "reconcile"
    assert await hub.resolve("Sim101") is True  # still there: paused again
    assert "stray_orders" in hub.states["Sim101"].mismatch
    fake.orders = []  # the user cancelled it in NinjaTrader
    assert await hub.resolve("Sim101") is True
    assert hub.states["Sim101"].mismatch is None
    halts = [r.payload["kind"] for r in rows(conn, "halt")]
    assert halts == ["reconcile", "resolve", "reconcile", "resolve"]


async def test_resolve_needs_a_running_account_and_ninjatraders_book(
    hub, conn, armed, client, monkeypatch
):
    await hub.tick(T0)
    with pytest.raises(KeyError):
        await hub.resolve("Sim999")
    hub.states["Sim101"] = replace(hub.states["Sim101"], mismatch="NinjaTrader mismatch: x")

    async def down(account):
        raise httpx.ConnectError("add-on unreachable")

    monkeypatch.setattr(client, "positions", down)
    assert await hub.resolve("Sim101") is False  # nothing lifted without a fresh read
    assert hub.states["Sim101"].mismatch == "NinjaTrader mismatch: x"


async def test_an_unreconciled_account_reconciles_on_a_tick_while_linked(
    hub, conn, armed, fake, monkeypatch
):
    """Armed while the link is up, restarted, or its reconcile read failed: the next tick."""
    unreconciled(monkeypatch)
    await hub.handle_stream({"type": "connected"})  # before this account started
    await hub.tick(T0)
    assert hub.states["Sim101"].unreconciled is None


async def test_reconcile_clears_a_replayed_flatten_that_was_refused(
    settings, hub, client, conn, armed, fake
):
    """A restart restores `exiting` from the sent Flatten's row, even after its `send failed`."""
    await hub.tick(T0)
    fake.rejects = ["refused"]
    await hub.handle_fill(FOREIGN)
    await hub.stop()
    restarted = LiveHub(settings, addon=client)
    try:
        await restarted.tick(T0 + timedelta(minutes=1))
        assert restarted.states["Sim101"].exiting == FLATTEN["client_id"]
        fake.positions = [HELD]  # NinjaTrader still holds the foreign fill
        await restarted.handle_stream({"type": "connected"})
        assert restarted.states["Sim101"].exiting != FLATTEN["client_id"]
    finally:
        await restarted.stop()


async def test_a_duplicate_flatten_with_a_position_still_held_is_a_failed_send(
    hub, conn, armed, fake
):
    """The first attempt was refused, its reply lost: `exiting` must not hold back later exits."""
    await hub.tick(T0)
    fake.seen.add(FLATTEN["client_id"])
    fake.positions = [HELD]
    await hub.handle_fill(FOREIGN)
    alerts = [r.payload["message"] for r in rows(conn, "alert")]
    assert alerts[-1] == (
        f"send {FLATTEN['client_id']} failed: duplicate, but NinjaTrader still holds a position"
    )
    assert hub.states["Sim101"].exiting is None


async def test_a_duplicate_flatten_with_ninjatrader_flat_has_landed(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.seen.add(FLATTEN["client_id"])
    await hub.handle_fill(FOREIGN)
    assert hub.states["Sim101"].exiting == FLATTEN["client_id"]
    assert [r.payload["message"].split(":")[0] for r in rows(conn, "alert")] == ["flattening"]


async def test_a_duplicate_flatten_that_cannot_be_checked_is_a_failed_send(
    hub, conn, armed, fake, client, monkeypatch
):
    """Unknown counts as not landed: a second /flatten never over-closes."""

    async def down(account):
        raise httpx.ConnectError("add-on unreachable")

    await hub.tick(T0)
    monkeypatch.setattr(client, "positions", down)
    fake.seen.add(FLATTEN["client_id"])
    await hub.handle_fill(FOREIGN)
    assert hub.states["Sim101"].exiting is None


async def test_account_messages_and_heartbeats_reach_the_hub(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_stream({**ACCOUNT, "id": "Sim102", "is_sim": False})  # not running here
    await hub.handle_stream(ACCOUNT)
    assert hub.states["Sim101"].nt == core.NtAccount(50_100.0, 100.0, 0.0)
    assert hub.states["Sim101"].halted is None
    ts = now_ts()
    await hub.handle_stream({"type": "heartbeat", "ts": ts})
    assert hub.status()["nt"]["last_heartbeat"] == ts
    await hub.handle_stream({"type": "position", **HELD})  # reconcile reads the book itself
    await hub.handle_stream({"type": "order", **ENTRY})
    assert hub.states["Sim101"].position.qty == 0


async def test_stream_fills_are_booked(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_stream(mnq_fill(1, 20000.0, T0 + timedelta(seconds=1)))
    assert hub.states["Sim101"].position.qty == 1


async def test_one_accounts_slow_post_never_holds_back_anothers(
    hub, conn, armed, client, monkeypatch
):
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    second, post = asyncio.Event(), client.bracket

    async def spy(cid, account, *rest):
        if account == "Sim101":  # hangs until Sim102's post is out
            await asyncio.wait_for(second.wait(), 10)
        ack = await post(cid, account, *rest)
        second.set()
        return ack

    monkeypatch.setattr(client, "bracket", spy)
    await hub.handle_bar(bar(T0))
    assert [s.working is not None for s in hub.states.values()] == [True, True]
    assert "alert" not in kinds(conn)


async def test_the_hub_follows_the_addon_stream(hub, conn, armed, fake, monkeypatch):
    unreconciled(monkeypatch)
    await hub.tick(T0)
    task = asyncio.create_task(service.run_stream(hub))
    try:
        await until(lambda: hub.states["Sim101"].unreconciled is None)
        fake.push(ACCOUNT)
        await until(lambda: hub.states["Sim101"].nt is not None)
        fake.kick()
        await until(lambda: "alert" in kinds(conn))  # the lost link
        await until(lambda: hub.nt_connected and hub.states["Sim101"].unreconciled is None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_kill_disarms_all_and_flattens_all(hub, conn, armed, fake):
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    await hub.tick(T0)
    assert await hub.kill() == {"ok": True, "error": None}
    [(path, body)] = fake.requests
    assert path == "/flatten-all" and len(body["client_id"]) <= 24
    assert arming.list_armed(conn) == () and hub.states == {} and hub.children == {}
    for account in ("Sim101", "Dry1"):
        [row] = journal.rows(conn, account=account, kind="alert")
        assert row.payload["message"].startswith("kill switch: disarmed")
    await hub.kill()  # a second press is its own request
    assert fake.requests[1][1]["client_id"] != body["client_id"]


async def test_kill_without_ninjatrader_is_not_ok_while_sim_or_live_is_armed(settings, conn, armed):
    hub = LiveHub(settings)
    try:
        result = await hub.kill()
    finally:
        await hub.stop()
    assert result["ok"] is False and "not configured" in result["error"]
    assert arming.list_armed(conn) == ()  # disarmed all the same
    [row] = rows(conn, "alert")
    assert "not configured" in row.payload["message"]


async def test_kill_with_only_dry_runs_needs_no_ninjatrader(settings, conn, armed):
    arming.delete_armed(conn, "Sim101")
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    hub = LiveHub(settings)
    try:
        assert await hub.kill() == {"ok": True, "error": None}
    finally:
        await hub.stop()
    assert arming.list_armed(conn) == ()


async def test_kill_is_not_ok_when_ninjatrader_cannot_flatten(
    hub, conn, armed, fake, client, monkeypatch
):
    fake.rejects = ["no connection to the broker"]
    assert await hub.kill() == {"ok": False, "error": "no connection to the broker"}
    arming.insert_armed(conn, armed)

    async def down(client_id):
        raise httpx.ConnectError("add-on unreachable")

    monkeypatch.setattr(client, "flatten_all", down)
    result = await hub.kill()
    assert result["ok"] is False and "unreachable" in result["error"]


async def test_kill_flattens_all_even_when_the_disarm_fails(hub, conn, armed, fake, monkeypatch):
    await hub.tick(T0)

    def locked_delete(conn, account):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "delete_armed", locked_delete)
    result = await hub.kill()
    assert [path for path, _ in fake.requests] == ["/flatten-all"]
    assert result["ok"] is False and "database is locked" in result["error"]
    # still armed, so halted in memory: nothing new goes out until it is disarmed
    assert hub.states["Sim101"].halted.startswith("kill switch: ")
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/flatten-all"]


async def test_kill_journals_the_disarm_routes_halt_row(hub, conn, armed, fake):
    await hub.tick(T0)
    await hub.kill()
    [row] = rows(conn, "halt")
    assert row.payload == {"reason": "disarmed", "kind": None, "until": None}


async def test_kill_journals_the_halt_rows_before_it_disarms(hub, conn, armed, fake, monkeypatch):
    """As the disarm route: an account whose delete then fails, or that is re-armed in between,
    already has its halt row."""
    seen, delete = [], arming.delete_armed

    def checking(conn, account):
        seen.append([r.payload for r in journal.rows(conn, account=account, kind="halt")])
        delete(conn, account)

    monkeypatch.setattr(service, "delete_armed", checking)
    await hub.tick(T0)
    await hub.kill()
    assert seen == [[service.DISARMED]]


@pytest.mark.parametrize("failing", ["delete", "read"])
async def test_a_failed_kill_disarm_halts_an_account_armed_but_not_started_yet(
    hub, conn, armed, fake, monkeypatch, failing
):
    """Armed in the 5 s before the kill, so not running yet: the next tick starts it halted.
    Armed at the very moment the kill read `armed` counts as armed before it."""
    arming.insert_armed(conn, replace(armed, armed_at=T0))
    locked_db = {"on": True}

    def locked_delete(conn, account):
        raise sqlite3.OperationalError("database is locked")

    def locked_read(conn):
        if locked_db["on"]:
            raise sqlite3.OperationalError("database is locked")
        return arming.list_armed(conn)

    if failing == "delete":
        monkeypatch.setattr(service, "delete_armed", locked_delete)
    else:
        monkeypatch.setattr(service, "list_armed", locked_read)
    result = await hub.kill()
    assert result["ok"] is False and "disarm failed" in result["error"]
    locked_db["on"] = False  # the lock passed; the row stayed armed
    await hub.tick(T0)
    assert hub.states["Sim101"].halted is not None
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/flatten-all"]
    assert rows(conn, "halt")  # so a restart keeps it halted
    arming.insert_armed(conn, replace(armed, armed_at=T0 + timedelta(seconds=1)))  # re-armed
    await hub.tick(T0 + timedelta(seconds=5))
    assert hub.states["Sim101"].halted is None


async def test_kill_still_disarms_when_its_halt_rows_cannot_be_written(
    hub, conn, armed, fake, monkeypatch
):
    """A deleted row can neither run nor restart: the halt row's error is reported, no more."""
    await hub.tick(T0)
    locked(monkeypatch, "halt")
    result = await hub.kill()
    assert result["ok"] is False and "database is locked" in result["error"]
    assert "disarm failed" not in result["error"]
    assert arming.list_armed(conn) == () and hub.states == {}


async def test_kill_names_a_failed_runner_stop_as_such(hub, conn, armed, fake, monkeypatch):
    """Rows deleted, only stopping the runners failed: not a failed disarm, still halted."""
    await hub.tick(T0)

    async def broken(now):
        raise RuntimeError("cannot stop")

    monkeypatch.setattr(hub, "_sync", broken)
    result = await hub.kill()
    assert result["ok"] is False and "disarm failed" not in result["error"]
    assert "disarmed, but stopping the runners failed: RuntimeError" in result["error"]
    assert arming.list_armed(conn) == ()
    assert hub.states["Sim101"].halted.startswith("kill switch: disarmed, but stopping")


async def test_kill_without_ninjatrader_reads_running_accounts_when_the_disarm_fails(
    settings, conn, armed, monkeypatch
):
    hub = LiveHub(settings)
    try:
        await hub.tick(T0)  # Sim101 running

        def unreadable(conn):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(service, "list_armed", unreadable)
        result = await hub.kill()
    finally:
        await hub.stop()
    assert result["ok"] is False and "not configured" in result["error"]


async def test_kill_counts_a_duplicate_flatten_all_as_done(hub, armed, client, monkeypatch):
    async def duplicate(client_id):
        return Ack(False, True, None)

    monkeypatch.setattr(client, "flatten_all", duplicate)
    assert await hub.kill() == {"ok": True, "error": None}


STOP = {**ENTRY, "order_id": "2", "role": "stop", "action": "sell", "price": 19990.0}
TARGET = {**STOP, "order_id": "3", "role": "target", "price": 20020.0}


@pytest.mark.parametrize(
    ("before", "positions", "orders", "buffered"),
    [
        ([], [HELD], [STOP, TARGET], 1),  # an entry fill NinjaTrader's book already holds
        ([FOREIGN], [], [], -1),  # a closing fill NinjaTrader's book already holds
    ],
)
async def test_fills_buffered_during_the_reconnect_reconcile_are_not_booked_twice(
    hub, conn, armed, fake, before, positions, orders, buffered
):
    await hub.tick(T0 - timedelta(minutes=1))
    for f in before:
        await hub.handle_fill({**f, "ts": mnq_fill(1, 0.0, T0 - timedelta(seconds=50))["ts"]})
    await hub.handle_stream(LOST)
    fake.positions, fake.orders, sent = positions, orders, len(fake.requests)
    await hub.handle_stream({"type": "connected"})  # reads the book at T0
    await hub.handle_fill(mnq_fill(buffered, 20000.0, T0 - timedelta(seconds=1)))
    assert hub.states["Sim101"].position.qty == sum(p["qty"] for p in positions)
    assert len(fake.requests) == sent  # no Flatten for a phantom position
    assert rows(conn, "fill")[-1].payload["qty"] == buffered  # journalled all the same


async def test_a_tick_reconcile_takes_its_time_at_the_read(
    hub, conn, armed, fake, clock, monkeypatch
):
    """Strategy loads (or the lock) can hold a tick up long before it reads NinjaTrader's book:
    `read_at` is the read's time, so a fill already in that book is not booked twice."""
    unreconciled(monkeypatch)
    spawn = hub._spawn

    async def slow(a):
        await spawn(a)
        clock["t"] += timedelta(seconds=45)

    monkeypatch.setattr(hub, "_spawn", slow)
    fake.positions, fake.orders = [HELD], [STOP, TARGET]
    hub.nt_connected = True  # linked: the tick reconciles the account it starts
    await hub.tick(T0)
    assert hub.states["Sim101"].read_at == T0 + timedelta(seconds=45)
    await hub.handle_fill(mnq_fill(1, 20000.0, T0 + timedelta(seconds=20)))  # in that book
    assert hub.states["Sim101"].position.qty == 1


async def test_start_follows_the_addon_stream(hub, armed):
    hub.start()
    await until(lambda: hub.nt_connected)


async def test_a_crashed_stream_follower_marks_the_link_down_and_restarts(
    hub, conn, armed, client, monkeypatch, caplog
):
    calls = []

    async def crashing():
        calls.append(1)
        yield {"type": "connected"}
        raise RuntimeError("bug in the stream")

    monkeypatch.setattr(client, "stream", crashing)
    monkeypatch.setattr(service, "RESTREAM_AFTER", 0.01)
    await hub.tick(T0)
    task = asyncio.create_task(service.run_stream(hub))
    try:
        await until(lambda: len(calls) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    [alert, *_] = rows(conn, "alert")  # paused as a lost link, with the error
    assert alert.payload["message"].startswith(core.LINK_LOST)
    # exactly the type: the error text could hold the stream URL's token
    assert alert.payload["message"] == f"{core.LINK_LOST} (stream follower failed: RuntimeError)"
    assert "bug in the stream" not in caplog.text
