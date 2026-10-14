"""#219: an end_of_day roll raises the drawdown floor from NinjaTrader's closing balance; a gap
between that balance and the app's own alone is never a breach. NinjaTrader booked P&L the app
never saw as a fill (a manual trade on another instrument, an exit while the server was down):
it halts with "confirm balances" instead, open-ended, until re-armed. A real breach still is one.
#222: the same for an intraday peak raise from NinjaTrader's account figures. #229: under that
halt, the flatten of a held position is one at a time, and whatever NinjaTrader holds goes too.
#225: a later-day restart under that halt checks NinjaTrader's floor, journalled with the halt."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from live_helpers import ARMED_AT, pinned, stored

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


@pytest.mark.parametrize("rules_account", ["lucid-daily-50k", "lucid-flex-50k"])
async def test_a_position_booked_while_the_gap_flatten_is_out_is_flattened(
    settings, client, conn, armed, fake, rules_account
):
    """Review of #231, blocking 1 (hub probe): the gap halt with the app flat and NinjaTrader at
    -500 open sends one account-wide /flatten, which the add-on accepts but does not fill. A
    manual MNQ buy is then booked: it gets its own /flatten at once, journalled as having no
    known stop, and no storm follows over falling bars, ticks and account messages."""
    if rules_account == "lucid-daily-50k":  # intraday: NinjaTrader's equity 52,200
        arming.insert_armed(conn, pinned(replace(armed, rules_account=rules_account)))
        figures, roll, at = account(52_700.0, realized=2700.0), T0 + timedelta(minutes=1), T0
    else:  # end_of_day: NinjaTrader's balance 52,200 at the close
        figures, roll, at = account(52_200.0, realized=2200.0), D2, D2
    hub = LiveHub(settings, addon=client)
    try:
        await started(hub)
        sent = len(fake.requests)
        await hub.handle_stream(figures | {"unrealized_pnl": -500.0})  # intraday: flattens (#235)
        await hub.handle_bar(bar(roll))
        s = hub.states["Sim101"]
        assert s.halted.startswith("confirm balances: a gap ") and s.position.qty == 0
        [one] = flattens(fake, sent)
        sent = len(fake.requests)
        await hub.handle_fill(fill(1, 20000.0, at + timedelta(minutes=2, seconds=5)))
        s = hub.states["Sim101"]
        [two] = flattens(fake, sent)
        assert s.position.qty == 1 and s.exiting == two != one
        rows = journal.rows(conn, account="Sim101", kind="action")
        assert [r.payload["reason"] for r in rows if r.payload["client_id"] == two] == [
            "position 1 has no known stop"
        ]
        for n, close in enumerate((19950.0, 19900.0, 19850.0), start=3):
            await hub.handle_bar(bar(at + timedelta(minutes=n), close))
            await hub.tick(at + timedelta(minutes=n, seconds=30))
            await hub.handle_stream(figures | {"unrealized_pnl": -3500.0})
        assert flattens(fake, sent) == [two]
    finally:
        await hub.stop()


BOOM = """
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    raise RuntimeError("boom")
"""


async def test_an_intraday_gap_under_a_strategy_error_halts_alerts_and_flattens(
    settings, client, conn, armed, fake, clock, tmp_path
):
    """#235, the review of #231 (round 2) probe on a Lucid Daily 50k: the strategy raises on the
    first bar (an open-ended halt that is no breach), then NinjaTrader shows 52,700 with -500
    open (a gap the app never saw). The gap halt replaces the strategy error: journalled, an
    alert naming the error, and one account-wide /flatten with the gap's reason, on that message
    with no bar since (item 2). A restart that day restores the gap halt, not the strategy
    error."""
    stored(settings.data_dir)  # warm-up (#79) finds yesterday, so the strategy runs
    path = tmp_path / "boom.py"
    path.write_text(BOOM)
    boom = replace(armed, strategy_path=str(path), strategy_hash=file_hash(path))
    arming.insert_armed(conn, pinned(replace(boom, rules_account="lucid-daily-50k")))
    figures = account(52_700.0, realized=2700.0) | {"unrealized_pnl": -500.0}
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_bar(bar(T0 + timedelta(minutes=1)))
        error = first.states["Sim101"].halted
        assert error.startswith("strategy error: ") and "boom" in error
        sent = len(fake.requests)
        await first.handle_stream(figures)  # no bar since: a feed outage holds nothing back
        [one] = flattens(fake, sent)
        await first.handle_bar(bar(T0 + timedelta(minutes=2)))
        s = first.states["Sim101"]
        assert s.halted.startswith("confirm balances: a gap on NinjaTrader's account figures: ")
        assert (s.halt_kind, s.acct.floor) == (None, -2000.0)
        assert [h["reason"] for h in halts(conn)] == [error, s.halted]
        # NinjaTrader's raised peak is journalled on this path too (review of #238): 52,700 - 500
        assert [r.payload.get("nt_peak") for r in journal.rows(conn, kind="peak")] == [52_200.0]
        alerts = [r.payload["message"] for r in journal.rows(conn, account="Sim101", kind="alert")]
        assert f"{s.halted} (was halted: {error})" in alerts
        assert flattens(fake, sent) == [one]
        rows = journal.rows(conn, account="Sim101", kind="action")
        assert [r.payload["reason"] for r in rows if r.payload.get("client_id") == one] == [
            s.halted
        ]
        for n in range(3, 6):  # not filled: no storm
            await first.handle_bar(bar(T0 + timedelta(minutes=n)))
            await first.handle_stream(figures)
        assert flattens(fake, sent) == [one]
    finally:
        await first.stop()
    clock["t"] = T0 + timedelta(hours=1)
    second = LiveHub(settings, addon=client)
    try:
        await second.handle_stream({"type": "connected"})
        await second.tick(clock["t"])
        await second.handle_stream(figures)
        s = second.states["Sim101"]
        assert (s.halted, s.halt_kind) == (halts(conn)[1]["reason"], None)
    finally:
        await second.stop()


# the clause of every breach alert that replaces a gap halt (review of #238)
NOT_THE_GAP = ", a loss through the floor the runner checks, not the gap alone"
THU = T0 + timedelta(days=3, hours=-0.5)  # Thursday 09:30 ET, trading day 2026-10-15


async def restarted(settings, client, clock, at, balance):
    """A new hub at `at`, started by NinjaTrader's `balance` and reconciled (flat)."""
    clock["t"] = at
    h = LiveHub(settings, addon=client)
    await h.handle_stream({"type": "connected"})
    await h.tick(at)
    await h.handle_stream(account(balance))
    await h.tick(at + timedelta(seconds=5))
    return h


async def test_a_later_day_restart_under_the_gap_halt_checks_ninjatraders_floor(
    settings, client, conn, armed, fake, clock
):
    """#225's probe: the gap halt at the 2026-10-13 roll (the app at 50,000, NinjaTrader at
    52,200, its floor 50,100), then a restart on Thursday with NinjaTrader at 52,200. The restart
    opens at NinjaTrader's balance, so it takes NinjaTrader's journalled peak too: room 2,100,
    not 4,200, and a manual 10 MNQ loss of -2,510 (NinjaTrader at 49,690, through its floor)
    breaches. It used to leave the floor at 48,000: no breach. A second restart that day opens
    as the first did, at NinjaTrader's figures."""
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_stream(account(52_200.0, realized=2200.0))
        await first.handle_bar(bar(D2))  # the gap halt
        reason = first.states["Sim101"].halted
    finally:
        await first.stop()
    second = await restarted(settings, client, clock, THU, 52_200.0)
    try:
        s = second.states["Sim101"]
        assert s.started and (s.halted, s.halt_kind) == (reason, None)
        assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2200.0, 2200.0, 100.0)
        [acc] = second.status()["accounts"]
        assert acc["limits"]["drawdown_remaining"] == 2100.0
    finally:
        await second.stop()
    third = await restarted(settings, client, clock, THU + timedelta(hours=1), 52_200.0)
    try:
        s = third.states["Sim101"]
        assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2200.0, 2200.0, 100.0)
        at = clock["t"]
        await third.handle_fill(fill(10, 20000.0, at + timedelta(seconds=10)))
        await third.handle_fill(fill(-10, 19875.0, at + timedelta(seconds=20)))  # -2,500 - $10
        await third.handle_stream(account(49_690.0, realized=-2510.0))
        await third.handle_bar(bar(at + timedelta(minutes=1)))
        s = third.states["Sim101"]
        assert s.halt_kind == "breach"
        assert s.halted == "breach max_drawdown: equity -310.00 at or below drawdown floor 100.00"
        # a same-day restart after the later-day one: the same clause (review of #238, round 2)
        alerts = [r.payload["message"] for r in journal.rows(conn, account="Sim101", kind="alert")]
        assert f"{s.halted}{NOT_THE_GAP} (was halted: {reason})" in alerts
    finally:
        await third.stop()
    [row] = [r.payload for r in journal.rows(conn, kind="peak")]  # the gap roll's, both peaks
    assert (row["peak"], row["nt_peak"]) == (50_000.0, 52_200.0)


async def test_a_same_day_restart_under_the_gap_halt_keeps_the_apps_peak(
    settings, client, conn, armed, fake, clock
):
    """The other side (#221's M5): a restart the day of the gap halt rebuilds the app's own
    balance from today's open, so NinjaTrader's journalled peak is not taken; its floor would
    breach the app's figures. The app's own loss through its own floor still breaches."""
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_stream(account(52_200.0, realized=2200.0))
        await first.handle_bar(bar(D2))
        reason = first.states["Sim101"].halted
    finally:
        await first.stop()
    second = await restarted(settings, client, clock, D2 + timedelta(hours=1), 52_200.0)
    try:
        at = clock["t"]
        await second.handle_bar(bar(at))
        s = second.states["Sim101"]
        assert s.halted.startswith("confirm balances: a gap ") and s.halt_kind is None
        assert (s.acct.balance, s.acct.peak, s.acct.floor) == (0.0, 0.0, -2000.0)
        await second.handle_fill(fill(10, 20000.0, at + timedelta(seconds=70)))
        await second.handle_fill(fill(-10, 19900.0, at + timedelta(seconds=80)))  # -2,010
        await second.handle_bar(bar(at + timedelta(minutes=2)))
        s = second.states["Sim101"]
        assert (
            s.halted == "breach max_drawdown: equity -2010.00 at or below drawdown floor -2000.00"
        )
        # one clause for every breach that replaces a gap halt, whose figures it is on aside
        alerts = [r.payload["message"] for r in journal.rows(conn, account="Sim101", kind="alert")]
        assert f"{s.halted}{NOT_THE_GAP} (was halted: {reason})" in alerts
    finally:
        await second.stop()


# #239: NinjaTrader's own equity against its own floor while its peak stays above the app's
WED = T0 + timedelta(days=2, hours=-0.5)  # Wednesday 09:30 ET, trading day 2026-10-14
NT_FLOOR = "confirm balances: a gap halt journalled NinjaTrader's peak 52200.00: "


async def test_a_loss_through_ninjatraders_own_floor_after_the_gap_shrank_is_caught(
    settings, client, conn, armed, fake, clock
):
    """M13 (review of #238) on a real `LiveHub`: the gap halt at Tuesday's roll (NinjaTrader at
    52,200, its floor 50,100), NinjaTrader closes Tuesday at 51,000 and Wednesday's roll takes
    that raise (the app's floor 49,000). A manual 10 MNQ loss of -910 puts NinjaTrader at 50,090,
    through its own floor, the app at -910 above its own. Running, the message alerts and the
    room shown is NinjaTrader's (-10); a same-day restart does the same on its first message.
    It used to say nothing, with the app's 90 of room shown."""
    first = LiveHub(settings, addon=client)
    try:
        await started(first)
        await first.handle_stream(account(52_200.0, realized=2200.0))
        await first.handle_bar(bar(D2))  # the gap halt
        reason = first.states["Sim101"].halted
        await first.handle_stream(account(51_000.0, realized=-1200.0))
        clock["t"] = WED
        await first.handle_bar(bar(WED))  # the roll takes NinjaTrader's 51,000
        await first.handle_stream(account(51_000.0))
        s = first.states["Sim101"]
        assert (s.halted, s.acct.peak, s.acct.floor) == (reason, 1000.0, -1000.0)
        await first.handle_fill(fill(10, 20000.0, WED + timedelta(seconds=10)))
        await first.handle_fill(fill(-10, 19955.0, WED + timedelta(seconds=20)))  # -900 - $10
        await first.handle_stream(account(50_090.0, realized=-910.0))
        s = first.states["Sim101"]
        assert (s.halted, s.halt_kind, s.acct.balance) == (reason, None, -910.0)
        [acc] = first.status()["accounts"]
        assert acc["limits"]["drawdown_remaining"] == -10.0
    finally:
        await first.stop()
    second = await restarted(settings, client, clock, WED + timedelta(hours=1), 50_090.0)
    try:
        s = second.states["Sim101"]
        assert s.started and (s.halted, s.acct.balance, s.acct.floor) == (reason, -910.0, -1000.0)
        [acc] = second.status()["accounts"]
        assert acc["limits"]["drawdown_remaining"] == -10.0
    finally:
        await second.stop()
    alerts = [r.payload["message"] for r in journal.rows(conn, account="Sim101", kind="alert")]
    caught = [m for m in alerts if m.startswith(NT_FLOOR)]
    assert len(caught) == 2  # once per run
    assert caught[0].startswith(
        f"{NT_FLOOR}NinjaTrader's equity 50090.00 is at or below its own drawdown floor 50100.00"
    )
    assert caught[0].endswith(f" (still halted: {reason})")
    assert [r["reason"] for r in halts(conn)] == [reason]  # no other halt row, and no breach
