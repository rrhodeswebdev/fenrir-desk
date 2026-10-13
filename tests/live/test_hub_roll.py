"""#219: an end_of_day roll raises the drawdown floor from NinjaTrader's closing balance; a gap
between that balance and the app's own alone is never a breach. NinjaTrader booked P&L the app
never saw as a fill (a manual trade on another instrument, an exit while the server was down):
it halts with "confirm balances" instead, open-ended, until re-armed. A real breach still is one.
#222: the same for an intraday peak raise from NinjaTrader's account figures. #229: under that
halt, the flatten of a held position is one at a time, and whatever NinjaTrader holds goes too."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from live_helpers import ARMED_AT, pinned

from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.core.bars import Bar
from fenrir.live import arming, journal, service
from fenrir.live.arming import Armed
from fenrir.live.service import LiveHub
from fenrir.settings import Settings
from fenrir.strategy import file_hash

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26
D2 = T0 + timedelta(days=1, hours=-0.5)  # Tuesday 09:30 ET, trading day 2026-10-13

IDLE = """
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    return state + 1, []
"""


def bar(ts: datetime, close: float = 20000.0) -> Bar:
    high = max(close, 20005.0)
    return Bar(ts, 20000.0, high, min(close, 19995.0), close, 10, "NQZ26")


def account(balance: float, realized: float = 0.0) -> dict:
    """An add-on "account" stream message for Sim101, absolute $."""
    return {
        "type": "account",
        "id": "Sim101",
        "name": "Sim101",
        "is_sim": True,
        "balance": balance,
        "realized_pnl_today": realized,
        "unrealized_pnl": 0.0,
    }


def fill(qty: int, price: float, ts: datetime) -> dict:
    """An add-on "fill" stream message on Sim101's MNQ front month, not Fenrir's own order."""
    stamp = ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "type": "fill",
        "account": "Sim101",
        "client_id": "other",
        "order_id": "o",
        "instrument": "MNQ 12-26",
        "qty": qty,
        "price": price,
        "commission": 0.0,
        "ts": stamp,
    }


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
    """Sim101 armed in sim, Lucid Flex 50k (end_of_day, $2,000, locks at +100), MNQ, phase start
    and peak $50,000: floor $48,000, and $50,100 once NinjaTrader closes a day at $52,100+."""
    path = tmp_path / "idle.py"
    path.write_text(IDLE)
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
            start_balance=50_000.0,
            peak_balance=50_000.0,
        )
    )
    arming.insert_armed(conn, a)
    return a


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


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


async def started(hub, balance=50_000.0, realized=0.0):
    """Sim101 started by NinjaTrader's first message and reconciled with its (flat) book."""
    await hub.handle_stream({"type": "connected"})
    await hub.tick(T0)
    await hub.handle_stream(account(balance, realized))
    await hub.tick(T0 + timedelta(seconds=5))


def halts(conn):
    return [r.payload for r in journal.rows(conn, account="Sim101", kind="halt")]


async def test_a_gain_the_app_never_saw_is_no_breach_at_the_roll(hub, conn, armed, fake):
    """The issue's probe: the app at $50,000, NinjaTrader closes the day at $52,200 (a gain the
    app never booked). The roll's floor from NinjaTrader, $50,100, is above the app's balance:
    this used to be `breach max_drawdown: equity 0.00 at or below drawdown floor 100.00`."""
    await started(hub)
    await hub.handle_stream(account(52_200.0, realized=2200.0))
    await hub.handle_bar(bar(D2))  # rolls the day, then marks the bar
    await hub.handle_bar(bar(D2 + timedelta(minutes=1)))
    s = hub.states["Sim101"]
    assert s.halt_kind is None and s.halt_day is None  # open-ended, never a breach
    assert s.halted == (
        "confirm balances: a gap at the end-of-day session close: NinjaTrader's balance 52200.00"
        " puts the drawdown floor at 50100.00, at or above the app's equity 50000.00 (P&L the app"
        " never booked: a trade on another instrument, or a fill while the server was down); any"
        " position the app holds is flattened, and the whole account while NinjaTrader shows"
        " open P&L; re-arm with the confirmed balances"
    )
    assert [h["kind"] for h in halts(conn)] == [None]
    assert [r.payload["peak"] for r in journal.rows(conn, kind="peak")] == [50_000.0]
    # the app keeps its own figures and floor: NinjaTrader's floor alone would breach them
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (0.0, 0.0, -2000.0)
    [acc] = hub.status()["accounts"]
    assert (acc["halted"], acc["halt_kind"], acc["nt_balance"]) == (True, None, 52_200.0)
    assert fake.requests == []  # flat: nothing to flatten


async def test_after_a_restart_the_gap_halt_holds_and_still_never_breaches(
    settings, client, conn, armed, clock
):
    """The roll journals the app's own peak, not NinjaTrader's: a restart that day rebuilds the
    same figures (no breach on its first bar) and restores the open-ended halt."""
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_stream(account(52_200.0, realized=2200.0))
        await first.handle_bar(bar(D2))
        reason = first.states["Sim101"].halted
    finally:
        await first.stop()
    clock["t"] = D2 + timedelta(hours=1)
    second = LiveHub(settings, addon=client)
    try:
        await second.handle_stream({"type": "connected"})
        await second.tick(clock["t"])
        await second.handle_stream(account(52_200.0))
        await second.handle_bar(bar(clock["t"]))
        s = second.states["Sim101"]
        assert s.started and (s.halted, s.halt_kind) == (reason, None)
        assert (s.acct.balance, s.acct.peak, s.acct.floor) == (0.0, 0.0, -2000.0)
        assert [h["kind"] for h in halts(conn)] == [None]
    finally:
        await second.stop()


async def test_a_gap_that_leaves_the_app_above_the_floor_only_raises_it(hub, conn, armed):
    """NinjaTrader $300 above the app (its gain of $2,200 against the app's $1,900): the floor
    rises to NinjaTrader's $50,100, stricter, and the app at $51,900 is still above it."""
    await started(hub, 51_900.0, realized=1900.0)
    await hub.handle_stream(account(52_200.0, realized=2200.0))
    await hub.handle_bar(bar(D2))
    s = hub.states["Sim101"]
    assert s.halted is None and halts(conn) == []
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (1900.0, 2200.0, 100.0)


async def test_a_gap_that_puts_the_floor_exactly_at_the_app_balance_halts(hub, conn, armed):
    """At the floor is a breach for the rules, so it is the gap's too: confirm balances."""
    await started(hub, 50_100.0, realized=100.0)
    await hub.handle_stream(account(52_200.0, realized=2200.0))
    await hub.handle_bar(bar(D2))
    s = hub.states["Sim101"]
    assert s.halted.startswith("confirm balances: ") and s.halt_kind is None
    assert s.acct.floor == -1900.0  # the app's own, trailed to its $100 at the close


async def test_a_real_drawdown_below_ninjatraders_floor_still_breaches(hub, conn, armed):
    """The app saw the gain (no gap): the roll raises the floor to $50,100 from NinjaTrader's
    close, and a real $2,110 loss the next day (fills and commission) breaches it."""
    await started(hub, 52_200.0, realized=2200.0)
    await hub.handle_bar(bar(D2))
    assert hub.states["Sim101"].acct.floor == 100.0
    await hub.handle_fill(fill(10, 20000.0, D2 + timedelta(seconds=70)))
    await hub.handle_fill(fill(-10, 19895.0, D2 + timedelta(seconds=80)))
    await hub.handle_stream(account(50_090.0, realized=-2110.0))  # NinjaTrader agrees
    await hub.handle_bar(bar(D2 + timedelta(minutes=2)))
    s = hub.states["Sim101"]
    assert s.halt_kind == "breach" and s.halt_day is None
    assert s.halted == "breach max_drawdown: equity 90.00 at or below drawdown floor 100.00"


async def test_a_real_drawdown_below_the_apps_own_floor_breaches_after_a_gap_halt(hub, conn, armed):
    """The gap halt keeps the app's own floor, $48,000, and its drawdown checks: a real loss
    through it is still a breach, which replaces the open-ended halt."""
    await started(hub)
    await hub.handle_stream(account(52_200.0, realized=2200.0))
    await hub.handle_bar(bar(D2))
    await hub.handle_fill(fill(10, 20000.0, D2 + timedelta(seconds=70)))
    await hub.handle_fill(fill(-10, 19900.0, D2 + timedelta(seconds=80)))  # -$2,000 - $10
    await hub.handle_bar(bar(D2 + timedelta(minutes=2)))
    s = hub.states["Sim101"]
    assert s.halt_kind == "breach"
    assert s.halted == "breach max_drawdown: equity -2010.00 at or below drawdown floor -2000.00"
    assert [h["kind"] for h in halts(conn)] == [None, "breach"]


HELD = {"account": "Sim101", "instrument": "MNQ 12-26", "qty": 10, "avg_price": 20000.0}
STOP = {
    "account": "Sim101",
    "order_id": "s1",
    "client_id": "x",
    "instrument": "MNQ 12-26",
    "role": "stop",
    "action": "sell",
    "qty": 10,
    "price": 19000.0,
    "state": "Working",
}


@pytest.mark.parametrize("unrealized", [-2140.0, 0.0])
async def test_a_position_held_over_a_gap_close_is_flattened_and_breaches(
    hub, conn, armed, fake, unrealized
):
    """Review of #221 at 6a33e18, hub probe: the app books +2,190 after commission, NinjaTrader
    is at 52,250 (a $60 gap) and its book holds 10 MNQ long with a stop at 19,000, adopted on
    reconnect, -2,140 open at the close with the link lost over it. NinjaTrader's floor is
    50,100. NinjaTrader reports that loss (its equity $10 above its floor) or a stale 0. The app
    must not ride the loss on its own floor (48,050) under a halt: it is flattened at the roll and
    breaches, and the breach holds at -2,400, -3,000 and -4,000 open."""
    await started(hub)
    await hub.handle_fill(fill(10, 20000.0, T0 + timedelta(seconds=10)))
    await hub.handle_fill(fill(-10, 20110.0, T0 + timedelta(seconds=20)))  # +2,200 - $10
    await hub.handle_bar(bar(T0 + timedelta(minutes=1)))  # the exit's own bar
    fake.positions, fake.orders = [HELD], [STOP]
    await hub.handle_stream({"type": "connected"})  # reconciles: adopts NinjaTrader's long
    await hub.handle_bar(bar(T0 + timedelta(minutes=2), close=19893.0))  # -2,140 open
    s = hub.states["Sim101"]
    assert (s.acct.balance, s.acct.open_pnl, s.position.qty) == (2190.0, -2140.0, 10)
    await hub.handle_stream({"type": "disconnected", "error": "stream closed"})
    await hub.handle_stream(account(52_250.0, realized=2250.0) | {"unrealized_pnl": unrealized})
    sent = len(fake.requests)
    await hub.handle_bar(bar(D2, close=19893.0))  # the roll, still -2,140 open
    assert "/flatten" in [path for path, _ in fake.requests[sent:]]
    for close in (19880.0, 19850.0, 19800.0):  # -2,400, -3,000, -4,000 open
        await hub.handle_bar(bar(D2 + timedelta(minutes=1 + (20000 - close) // 100), close))
        s = hub.states["Sim101"]
        assert s.halt_kind == "breach" and s.halted.startswith("breach max_drawdown: ")


def flattens(fake, since=0):
    """The client ids of the /flatten posts from request `since` on."""
    return [body["client_id"] for path, body in fake.requests[since:] if path == "/flatten"]


async def test_the_gap_flatten_is_one_at_a_time_and_follows_reconciles_and_restarts(
    settings, client, conn, armed, fake, clock
):
    """Review of #221 (round 3), non-blocking 2 (K1, K7, K10): the app at +990 adopts 10 MNQ
    long held at -900 over a close where NinjaTrader is at 52,250 (a $1,260 gap). The roll halts
    and flattens once; the add-on accepts the flatten but never fills it. Bars, ticks and account
    messages send no more while it is out. A reconcile (the link back, NinjaTrader's figures not
    yet) ends it, and the next bar flattens again under a new id; so does a restart that day."""
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_fill(fill(10, 20000.0, T0 + timedelta(seconds=10)))
        await first.handle_fill(fill(-10, 20050.0, T0 + timedelta(seconds=20)))  # +1,000 - $10
        await first.handle_bar(bar(T0 + timedelta(minutes=1)))  # the exit's own bar
        fake.positions, fake.orders = [HELD], [STOP]
        await first.handle_stream({"type": "connected"})  # adopts NinjaTrader's long
        await first.handle_bar(bar(T0 + timedelta(minutes=2), close=19955.0))  # -900 open
        await first.handle_stream({"type": "disconnected", "error": "stream closed"})
        held = account(52_250.0, realized=2250.0) | {"unrealized_pnl": -900.0}
        await first.handle_stream(held)
        sent = len(fake.requests)
        await first.handle_bar(bar(D2, close=19955.0))  # the roll
        s = first.states["Sim101"]
        assert s.halted.startswith("confirm balances: a gap at the end-of-day session close: ")
        assert (s.halt_kind, s.acct.floor, s.position.qty) == (None, -1010.0, 10)
        [one] = flattens(fake, sent)
        rows = journal.rows(conn, account="Sim101", kind="action")
        assert [r.payload["reason"] for r in rows if r.payload["client_id"] == one] == [s.halted]
        for n in range(1, 4):  # not filled: no storm over bars, ticks and account messages
            await first.handle_bar(bar(D2 + timedelta(minutes=n), close=19955.0))
            await first.tick(D2 + timedelta(minutes=n, seconds=30))
            await first.handle_stream(held)
        assert flattens(fake, sent) == [one]
        await first.handle_stream({"type": "disconnected", "error": "stream closed"})
        await first.handle_stream({"type": "connected"})  # reconciles: still long, no figures
        assert first.states["Sim101"].nt is None
        await first.handle_bar(bar(D2 + timedelta(minutes=5), close=19955.0))
        [again] = flattens(fake, sent)[1:]
        assert again != one
    finally:
        await first.stop()
    clock["t"] = D2 + timedelta(minutes=10)
    second = LiveHub(settings, addon=client)
    try:
        await second.handle_stream({"type": "connected"})
        await second.tick(clock["t"])
        await second.handle_stream(held)
        await second.tick(clock["t"] + timedelta(seconds=5))  # reconciles: adopts the long
        before = len(fake.requests)
        await second.handle_bar(bar(clock["t"], close=19955.0))
        s = second.states["Sim101"]
        assert s.halted.startswith("confirm balances: a gap ") and s.position.qty == 10
        assert len(flattens(fake, before)) == 1
    finally:
        await second.stop()


async def test_an_intraday_gain_the_app_never_saw_halts_and_survives_a_restart(
    settings, client, conn, armed, fake, clock
):
    """#222's probe on a Lucid Daily 50k (intraday, $2,000, locks at +100): the app at 50,000,
    NinjaTrader at 52,200. Its raise would put the floor at 50,100, above the app's equity: the
    gap's halt, no breach, nothing sent (flat, and NinjaTrader shows no open P&L). A restart that
    day rebuilds the app's figures and still never breaches."""
    daily = pinned(replace(armed, rules_account="lucid-daily-50k"))
    arming.insert_armed(conn, daily)
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_stream(account(52_200.0, realized=2200.0))
        await first.handle_bar(bar(T0 + timedelta(minutes=1)))
        s = first.states["Sim101"]
        assert s.halted == (
            "confirm balances: a gap on NinjaTrader's account figures: NinjaTrader's equity"
            " 52200.00 puts the drawdown floor at 50100.00, at or above the app's equity 50000.00"
            " (P&L the app never booked: a trade on another instrument, or a fill while the"
            " server was down); any position the app holds is flattened, and the whole account"
            " while NinjaTrader shows open P&L; re-arm with the confirmed balances"
        )
        assert (s.halt_kind, s.acct.floor) == (None, -2000.0)
        assert fake.requests == [] and [h["kind"] for h in halts(conn)] == [None]
    finally:
        await first.stop()
    clock["t"] = T0 + timedelta(hours=1)
    second = LiveHub(settings, addon=client)
    try:
        await second.handle_stream({"type": "connected"})
        await second.tick(clock["t"])
        await second.handle_stream(account(52_200.0, realized=2200.0))
        await second.handle_bar(bar(clock["t"]))
        s = second.states["Sim101"]
        assert s.started and (s.halted, s.halt_kind) == (halts(conn)[0]["reason"], None)
        assert (s.acct.balance, s.acct.peak, s.acct.floor) == (0.0, 0.0, -2000.0)
        assert [h["kind"] for h in halts(conn)] == [None] and fake.requests == []
    finally:
        await second.stop()
