"""#79 (21.5, DEC-4e): on start and restart `Ctx.bars` fills from the bar store plus today's live
bars, and `init` + `step` replay today with actions suppressed, `ctx.position` and
`ctx.account` from today's journalled fills; a replayed action that differs from the journalled
one halts the account ("warm-up diverged"). Live decisions start at the next closed bar."""

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from backtest_helpers import D1, D2, D3, NQ, bar, et, make_rules
from live_helpers import ARMED_AT, decide_now, fill, make_setup, pinned, reconciled, stored

from fenrir.backtest.attempt import MINUTE, WINDOW, run_attempt
from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.core.actions import Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import load_instruments
from fenrir.data.live import FeedStopped
from fenrir.data.store import load_bars, write_bars
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.core import DayStart, Start
from fenrir.live.service import LiveHub, run_bars
from fenrir.settings import Settings
from fenrir.strategy import Strategy, file_hash

LONG = Bracket("long", 1, 19990.0, 20020.0)
WARMING = "warming up: test"


def day_bars(day, n: int, first: str = "09:30", close: float = 20000.0) -> tuple[Bar, ...]:
    start = et(day, first)
    return tuple(
        Bar(start + i * MINUTE, close, close + 5, close - 5, close + i % 7, 100, "NQZ25")
        for i in range(n)
    )


def kinds(effects):
    return [e.kind if isinstance(e, core.Log) else "send" for e in effects]


def rows_of(effects, at):
    """Effects as journal rows (ts, kind, payload), written at `at`."""
    return [(at, e.kind, e.payload) for e in effects if isinstance(e, core.Log)]


def journalled(rows):
    """Rows as the journal hands them back: payloads through JSON, datetimes as str."""
    return [(ts, kind, json.loads(json.dumps(p, default=str))) for ts, kind, p in rows]


# --- AC1: step sees the same last <= 500 bars a backtest would ---


def test_warm_up_shows_step_the_bars_a_backtest_shows_it():
    history = day_bars(D1, 400) + day_bars(D2, 300)  # more than WINDOW, over two days
    today = day_bars(D3, 6)
    seen = []

    def record(state, b, ctx):
        seen.append(ctx.bars)
        return state, []

    strategy = Strategy("rec", Path("rec.py"), "0" * 64, "NQ", {}, lambda p: None, record)
    every = history + today
    run_attempt(every, len(history), len(every), strategy, make_rules(), NQ)
    s, _ = core.start(make_setup(make_rules()), today[0].ts)
    ctxs, window = core.warm_up(s, history, today[:5], ())
    assert [c.bars for c in ctxs] == [tuple(b) for b in seen[:5]]
    assert len(window) == WINDOW and window == ctxs[-1].bars
    # live decisions start at the next closed bar, on the backtest's window
    s, _ = core.warmed(replace(s, warming=WARMING), window, [(c.ts, ()) for c in ctxs], ())
    s, _ = core.on_bar(s, today[5])
    assert core.ctx(s).bars == tuple(seen[5]) and s.warming is None


def test_warm_up_never_steps_or_keeps_a_roll_window_bar():
    history = day_bars(D1, 3)
    roll = day_bars(D1.replace(month=12, day=15), 3)  # 2025-12-15: in Z25 -> H26's roll window
    s, _ = core.start(make_setup(make_rules()), roll[0].ts)
    ctxs, window = core.warm_up(s, history, roll, ())
    assert ctxs == () and window == history


def test_warm_up_position_and_account_come_from_todays_journalled_fills():
    setup = make_setup(
        make_rules(),
        mode="sim",
        start=Start(50_000.0, 50_000.0, 50_000.0),
        day_start=DayStart(50_000.0, 50_000.0),
    )
    today = day_bars(D3, 4)
    s = reconciled(core.start(setup, today[-1].ts + MINUTE)[0])
    # live: the strategy bracketed at bar 0, its entry filled during bar 1
    live = reconciled(core.start(setup, today[0].ts)[0])
    live, _ = core.on_bar(live, today[0])
    live, sent = decide_now(live, [LONG])
    cid = sent[-1].client_id
    entry = fill(1, 20000.0, today[1].ts + timedelta(seconds=20), cid=cid)
    rows = [
        *rows_of(sent, today[0].ts + MINUTE + timedelta(seconds=2)),
        (today[1].ts + timedelta(seconds=21), "fill", entry),
    ]
    ctxs, _ = core.warm_up(s, (), today, journalled(rows))
    assert [c.position.qty for c in ctxs] == [0, 1, 1, 1]
    assert (ctxs[1].position.stop, ctxs[1].position.target) == (LONG.stop, LONG.target)
    # the open position is marked at each bar, as live (close 20003 at bar 3: +$60)
    assert ctxs[3].account.today_pnl == 60.0 - setup.rules.costs.commission_per_side.NQ


def sim_setup():
    return make_setup(
        make_rules(),
        mode="sim",
        start=Start(50_000.0, 50_000.0, 50_000.0),
        day_start=DayStart(50_000.0, 50_000.0),
    )


def lived(setup, today, entry_at, bar_rows_at=timedelta(seconds=2)):
    """Today run live: a Bracket at bar 0, its entry fill at `entry_at`; each bar's rows
    written `bar_rows_at` after it closed (the feed's grace, or a hub lock held), in journal
    order. Returns the rows and the (position, today's P&L) each bar's live step saw."""
    s = reconciled(core.start(setup, today[0].ts)[0])
    rows, seen, filled = [], [], False
    for i, b in enumerate(today):
        written = b.ts + MINUTE + bar_rows_at
        if not filled and entry_at < written:  # the fill is booked before this bar's step
            entry = fill(1, 20000.0, entry_at, commission=2.5, cid=s.working.client_id)
            s, out = core.on_fill(s, entry)
            rows += rows_of(out, entry_at + timedelta(milliseconds=500))
            filled = True
        s, out = core.on_bar(s, b)
        seen.append((s.position.qty, core.ctx(s).account.today_pnl))
        s, more = core.decide(s, [LONG] if i == 0 else [], written, 180)
        rows += rows_of(out + more, written)
    return journalled(rows), seen


def test_warm_up_books_rows_in_the_order_live_booked_them():
    # review of #224: a fill booked in the grace before bar 1 reached step was in its position
    today = day_bars(D3, 4)
    entry_at = today[1].ts + MINUTE  # bar 1 has ended; its bar arrives 2 s later
    rows, seen = lived(sim_setup(), today, entry_at)
    assert [qty for qty, _ in seen] == [0, 1, 1, 1]
    s = reconciled(core.start(sim_setup(), today[-1].ts + MINUTE)[0])
    ctxs, _ = core.warm_up(s, (), today, rows)
    assert [(c.position.qty, c.account.today_pnl) for c in ctxs] == seen


def test_warm_up_books_by_time_the_rows_of_bars_never_journalled():
    # before arming, or while down: no "bar" row says where the bar was stepped
    today = day_bars(D3, 4)
    rows, _ = lived(sim_setup(), today, today[1].ts + timedelta(seconds=20))
    rows = [r for r in rows if not (r[1] == "bar" and r[2]["ts"] != str(today[0].ts))]
    s = reconciled(core.start(sim_setup(), today[-1].ts + MINUTE)[0])
    ctxs, _ = core.warm_up(s, (), today, rows)
    assert [c.position.qty for c in ctxs] == [0, 1, 1, 1]


def test_warm_up_replays_from_todays_open_not_the_position_held_now():
    # a restart holding today's position: the replay starts flat, at the open's balance
    today = day_bars(D3, 3)
    rows, seen = lived(sim_setup(), today, today[0].ts + timedelta(seconds=80))
    s = reconciled(core.start(sim_setup(), today[-1].ts + MINUTE)[0])
    s = core.replay(s, [(kind, p) for _, kind, p in rows])
    assert s.position.qty == 1 and s.acct.balance == -2.5  # the entry's commission, booked
    ctxs, _ = core.warm_up(s, (), today, rows)
    assert [(c.position.qty, c.account.today_pnl) for c in ctxs] == seen
    assert [qty for qty, _ in seen] == [0, 1, 1] and seen[0][1] == 0.0


# --- divergence ---


def stepped(effects, at):
    """`decide`'s effects as the journal hands them back, written at `at`."""
    return journalled(rows_of(effects, at))


def test_a_replay_that_matches_the_journal_warms_the_account():
    today = day_bars(D3, 2)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    _, effects = decide_now(s, [LONG, Cancel("x")])
    rows = stepped(effects, today[0].ts + MINUTE)
    rows += [(today[1].ts, "alert", {"message": "x"})]  # other rows never count
    warm = replace(s, warming=WARMING)
    out, logs = core.warmed(warm, today, [(today[0].ts, (LONG, Cancel("x")))], rows)
    assert logs == () and out.warming is None and out.bars == today and out.halted is None


def test_a_replayed_action_that_differs_from_the_journal_halts():
    today = day_bars(D3, 2)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    _, effects = decide_now(s, [LONG])  # journalled: a Bracket at bar 0
    rows = stepped(effects, today[0].ts + MINUTE)
    out, logs = core.warmed(
        replace(s, warming=WARMING), today, [(today[0].ts, ()), (today[1].ts, (LONG,))], rows
    )
    assert kinds(logs) == ["halt"] and out.halt_kind is None
    assert out.halted.startswith(f"warm-up diverged at {today[0].ts.isoformat()}: journalled [")
    assert out.halted.endswith(", replayed []")
    assert out.warming is None  # warmed all the same: the halt holds entries back


def test_a_later_action_that_differs_halts_too():
    today = day_bars(D3, 1)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    _, effects = decide_now(s, [LONG, Cancel("x")])
    rows = stepped(effects, today[0].ts + MINUTE)
    _, logs = core.warmed(s, today, [(today[0].ts, (LONG, Cancel("y")))], rows)
    assert kinds(logs) == ["halt"]


def test_only_the_bars_own_actions_count_not_the_runners_after_them():
    # a runner's Flatten (a stopless fill) journalled after the bar's actions is not step's
    today = day_bars(D3, 1)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    _, effects = decide_now(s, [LONG])
    rows = stepped(effects, today[0].ts + MINUTE)
    flatten = {"type": "Flatten", "mode": "sim", "sent": True, "client_id": "c", "reason": "x"}
    rows += [(today[0].ts + MINUTE * 1.5, "action", flatten)]
    _, logs = core.warmed(s, today, [(today[0].ts, (LONG,))], rows)
    assert logs == ()


def test_a_rejected_action_is_compared_as_the_strategy_returned_it():
    today = day_bars(D3, 1)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    s, _ = core.halt(s, "test")
    _, effects = decide_now(s, [LONG, Flatten()])  # both journalled: a rejection, an action
    assert kinds(effects) == ["bar", "rejection", "action"]
    rows = stepped(effects, today[0].ts + MINUTE)
    _, logs = core.warmed(s, today, [(today[0].ts, (LONG, Flatten()))], rows)
    assert logs == ()
    _, logs = core.warmed(s, today, [(today[0].ts, (Flatten(), LONG))], rows)
    assert logs == ()  # already halted open-ended: a later halt is not journalled
    s2 = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    _, logs = core.warmed(s2, today, [(today[0].ts, (Flatten(), LONG))], rows)
    assert kinds(logs) == ["halt"]


def test_bars_journalled_while_warming_or_never_journalled_are_not_compared():
    today = day_bars(D3, 2)
    s = core.on_bar(core.start(make_setup(make_rules()), today[0].ts)[0], today[0])[0]
    cold, effects = core.on_bar(replace(s, warming=WARMING), today[1])
    assert kinds(effects) == ["bar"] and effects[0].payload["warming"] is True
    rows = stepped(effects, today[1].ts + MINUTE)
    _, logs = core.warmed(cold, today, [(today[0].ts, (LONG,)), (today[1].ts, (LONG,))], rows)
    assert logs == ()


# --- warming: entries wait, exits keep working ---


def test_warming_blocks_entries_but_exits_keep_working():
    b = bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0, D1)
    s = core.on_bar(core.start(make_setup(make_rules()), b.ts)[0], b)[0]
    _, effects = decide_now(replace(s, warming=WARMING), [LONG, Flatten()])
    assert kinds(effects) == ["bar", "rejection", "action"]
    assert effects[1].payload["reason"] == f"paused: {WARMING}"
    status = core.account_status(replace(s, warming=WARMING))
    # a condition the strips show, under a stale pause too, but not a halt
    warming = {"kind": "warming", "reason": WARMING}
    assert (status["halt_reason"], status["halted"], status["conditions"]) == (
        WARMING,
        False,
        [warming],
    )
    stale = core.account_status(replace(s, warming=WARMING, stale="no NQ bar"))
    assert stale["conditions"] == [{"kind": "stale", "reason": "no NQ bar"}, warming]


def test_a_warming_account_still_flattens_at_flat_by():
    setup = make_setup(
        make_rules(),
        mode="sim",
        start=Start(50_000.0, 50_000.0, 50_000.0),
        day_start=DayStart(50_000.0, 50_000.0),
    )
    b = bar("15:00", 20000.0, 20005.0, 19995.0, 20000.0, D1)
    s = reconciled(core.start(setup, b.ts)[0])
    s, _ = core.on_fill(s, fill(1, 20000.0, b.ts + MINUTE))  # a position with no known stop...
    s = replace(s, exiting=None, position=replace(s.position, stop=19990.0))  # ...given one
    late = bar("16:40", 20000.0, 20005.0, 19995.0, 20000.0, D1)
    _, effects = core.on_bar(replace(s, warming=WARMING), late)
    assert kinds(effects)[-3:] == ["action", "send", "bar"]
    assert effects[-1].payload == {**asdict(late), "actions": 0, "warming": True}


# --- the hub: today's bars from the stream's replay, warm-up on start and restart ---

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26

# reports what step saw: its count of steps so far and the window, as a Cancel (never sent)
REPORT = """
from fenrir.core.actions import Cancel
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    seen = f"{state} {len(ctx.bars)} {ctx.bars[0].ts.isoformat()} {ctx.bars[-1].ts.isoformat()}"
    return state + 1, [Cancel(seen)]
"""

# enters once, on the first bar it steps, and never again
ONCE = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return False
def step(state, bar, ctx):
    return True, ([] if state else [Bracket("long", 1, bar.close - 10, bar.close + 20)])
"""

NEVER = ONCE.replace("([] if state else [", "([] if True else [")

ALWAYS = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    if bar.close < 0:
        raise ValueError("a negative close")
    return state + 1, [Bracket("long", 1, bar.close - 10, bar.close + 20)]
"""


def live_bar(minutes: int, close: float = 20000.0) -> Bar:
    """The NQZ26 bar opening `minutes` after T0."""
    return Bar(T0 + timedelta(minutes=minutes), close, close + 5, close - 5, close, 10, "NQZ26")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")
    stored(s.data_dir)
    return s


@pytest.fixture
def conn(settings):
    c = journal.open_db(settings.private_dir / "fenrir.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


def arm(conn, tmp_path, source: str, mode: str = "dry", **kw) -> Armed:
    path = tmp_path / "strategy.py"
    path.write_text(source)
    a = pinned(
        Armed(
            "Sim101",
            str(path),
            file_hash(path),
            "lucid-flex-50k",
            "MNQ",
            mode,
            "evaluation",
            ARMED_AT,
            **kw,
        )
    )
    arming.insert_armed(conn, a)
    return a


@pytest.fixture
async def hub(settings):
    h = LiveHub(settings)
    yield h
    await h.stop()


def journalled_kinds(conn):
    return [r.kind for r in journal.rows(conn, account="Sim101")]


def payloads(conn, kind):
    return [r.payload for r in journal.rows(conn, account="Sim101", kind=kind)]


async def replay(hub, *bars):
    """The stream (re)subscribes: today's bars so far replay, then a live tick ends it."""
    hub.resubscribed()
    for b in bars:
        await hub.handle_bar(b)
    hub.feed_live("NQZ26")


@pytest.mark.anyio
async def test_step_sees_the_backtests_bars_after_start(hub, settings, conn, tmp_path, clock):
    # AC1: the store's bars, then today's from the stream's replay, as a backtest's window
    await replay(hub, *(live_bar(m) for m in range(3)))
    arm(conn, tmp_path, REPORT)
    clock["t"] = T0 + timedelta(minutes=3)
    await hub.tick(clock["t"])
    assert journalled_kinds(conn) == []  # the replay journals nothing
    await hub.handle_bar(live_bar(3))
    assert journalled_kinds(conn) == ["bar", "action"]
    [seen] = [p["order_id"] for p in payloads(conn, "action")]
    yesterday = T0.date() - timedelta(days=1)  # today's bars come from the feed, not the store
    history = load_bars(settings.data_dir, load_instruments(), "NQ", end=yesterday)
    window = (*history, *(live_bar(m) for m in range(4)))[-WINDOW:]
    first, last = window[0].ts.isoformat(), window[-1].ts.isoformat()
    assert seen == f"3 {len(window)} {first} {last}"  # init, then 3 replayed steps


@pytest.mark.anyio
async def test_replaying_today_journals_and_sends_nothing(settings, conn, tmp_path, clock):
    # AC2: a sim account, its book reconciled; the replayed Brackets go nowhere
    fake = FakeAddon()
    async with running(fake) as url:
        client = AddonClient(url, fake.token, timeout=0.2, retries=2)
        hub = LiveHub(settings, addon=client)
        try:
            await replay(hub, *(live_bar(m) for m in range(3)))
            arm(conn, tmp_path, ALWAYS, "sim", start_balance=50_000.0, peak_balance=50_000.0)
            await hub.handle_stream({"type": "connected"})
            clock["t"] = T0 + timedelta(minutes=3)
            await hub.tick(clock["t"])
            message = {"type": "account", "id": "Sim101", "name": "Sim101", "is_sim": True}
            await hub.handle_stream(
                message | {"balance": 50_000.0, "realized_pnl_today": 0.0, "unrealized_pnl": 0.0}
            )
            await hub.tick(clock["t"])  # reconciles
            assert hub.states["Sim101"].warming is None
            assert not {"bar", "action", "rejection"} & set(journalled_kinds(conn))
            assert fake.requests == []
            await hub.handle_bar(live_bar(3))
            assert [path for path, _ in fake.requests] == ["/bracket"]
        finally:
            await hub.stop()
            await client.aclose()


@pytest.mark.anyio
async def test_a_restart_mid_session_does_not_enter_again(settings, conn, tmp_path, clock):
    # AC3: the strategy entered at the first bar; restarted, it knows
    arm(conn, tmp_path, ONCE)
    first = LiveHub(settings)
    try:
        for m in range(2):
            clock["t"] = T0 + timedelta(minutes=m)
            await first.handle_bar(live_bar(m))
    finally:
        await first.stop()
    assert journalled_kinds(conn) == ["bar", "action", "bar"]
    second = LiveHub(settings)
    try:
        await replay(second, live_bar(0), live_bar(1))
        clock["t"] = T0 + timedelta(minutes=2)
        await second.tick(clock["t"])
        await second.handle_bar(live_bar(2))
        assert second.states["Sim101"].halted is None  # the replay matched the journal
    finally:
        await second.stop()
    assert journalled_kinds(conn) == ["bar", "action", "bar", "bar"]


@pytest.mark.anyio
async def test_a_replay_that_differs_from_the_journal_halts(hub, conn, tmp_path, clock):
    arm(conn, tmp_path, NEVER)
    row = {**asdict(live_bar(0)), "actions": 1}
    journal.write(conn, T0 + MINUTE, "Sim101", "bar", row)
    order = {"type": "Bracket", "side": "long", "qty": 1, "stop": 19990.0, "target": 20020.0}
    journal.write(conn, T0 + MINUTE, "Sim101", "action", order | {"mode": "dry", "sent": False})
    await replay(hub, live_bar(0), live_bar(1))
    clock["t"] = T0 + timedelta(minutes=2)
    await hub.tick(clock["t"])
    s = hub.states["Sim101"]
    assert s.halted.startswith(f"warm-up diverged at {T0.isoformat()}: journalled [{{'type'")
    assert journalled_kinds(conn)[-1] == "halt"


@pytest.mark.anyio
async def test_rows_from_an_earlier_arming_are_not_compared(hub, conn, tmp_path, clock):
    a = arm(conn, tmp_path, NEVER)
    old = ARMED_AT - timedelta(minutes=30)  # journalled by the arming before this one
    row = {**asdict(live_bar(0)), "actions": 1}
    order = {"type": "Bracket", "side": "long", "qty": 1, "stop": 19990.0, "target": 20020.0}
    journal.write(conn, old, "Sim101", "bar", row)
    journal.write(conn, old, "Sim101", "action", order | {"mode": "dry", "sent": False})
    assert a.armed_at == ARMED_AT
    await replay(hub, live_bar(0), live_bar(1))
    clock["t"] = T0 + timedelta(minutes=2)
    await hub.tick(clock["t"])
    assert hub.states["Sim101"].halted is None


@pytest.mark.anyio
async def test_a_warm_up_replays_only_todays_bars(hub, conn, tmp_path, clock):
    friday = Bar(T0 - timedelta(days=3), 1.0, 1.0, 1.0, 1.0, 1, "NQZ26")  # Friday 10:00 ET
    await replay(hub, friday)  # the buffer still holds the last trading day's bars
    arm(conn, tmp_path, REPORT)
    await hub.tick(T0)
    await hub.handle_bar(live_bar(0))
    [seen] = [p["order_id"] for p in payloads(conn, "action")]
    assert seen.startswith("0 ")  # nothing replayed: Friday's bar is not today's


@pytest.mark.anyio
async def test_an_error_while_warming_up_never_reaches_the_feed(hub, conn, tmp_path, monkeypatch):
    # review of #224: from handle_bar it would reach run_bars, which resubscribes (a full replay)
    def broken(*args):
        raise RuntimeError("a bug")

    monkeypatch.setattr(core, "warm_up", broken)
    arm(conn, tmp_path, ALWAYS)
    await hub.handle_bar(live_bar(0))  # starts the account: the warm-up fails inside
    message = "warming up: failed (RuntimeError: a bug); no new entries until it succeeds"
    assert hub.states["Sim101"].warming == message
    assert payloads(conn, "alert") == [{"message": message}]
    assert "Sim101" in hub.children  # the strategy never stepped: a later tick retries
    monkeypatch.undo()
    await hub.tick(T0 + timedelta(minutes=1, seconds=5))
    assert hub.states["Sim101"].warming is None


@pytest.mark.anyio
async def test_an_error_after_the_replay_halts_that_account_and_never_leaves_the_bar(
    hub, conn, tmp_path, clock, monkeypatch
):
    def broken(*args):
        raise RuntimeError("a bug")

    monkeypatch.setattr(core, "warmed", broken)
    arm(conn, tmp_path, ALWAYS)
    await replay(hub, live_bar(0))
    await hub.handle_bar(live_bar(1))  # starts the account; the error stays inside
    s = hub.states["Sim101"]
    assert s.halted == "live runner error: RuntimeError: a bug"  # stepped: never again
    assert hub.children == {}
    monkeypatch.undo()
    await only_the_halt(hub, conn, clock)


@pytest.mark.anyio
async def test_a_warm_up_failing_before_it_steps_is_retried_at_most_once_a_minute(
    hub, conn, tmp_path, monkeypatch, caplog
):
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError("a bug")

    monkeypatch.setattr(core, "warm_up", broken)
    arm(conn, tmp_path, ALWAYS)
    with caplog.at_level(logging.WARNING, logger="fenrir.live.service"):
        for seconds in range(0, 60, 5):  # a tick every 5 s
            await hub.tick(T0 + timedelta(seconds=seconds))
        assert len(calls) == 1
        await hub.tick(T0 + timedelta(seconds=60))
    assert len(calls) == 2
    logged = [r for r in caplog.records if "warm-up failed" in r.getMessage()]
    assert [r.exc_info is not None for r in logged] == [True, False]  # a traceback, then a line
    assert len(payloads(conn, "alert")) == 1


@pytest.mark.anyio
async def test_a_failed_sync_on_a_bar_never_reaches_the_feed(hub, conn, tmp_path, monkeypatch):
    # review of #224: from handle_bar it would reach run_bars, which resubscribes (a full replay)
    arm(conn, tmp_path, ALWAYS)
    await hub.tick(T0)

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "list_armed", locked)
    await hub.handle_bar(live_bar(0))  # no raise; the running account still takes its bar
    assert journalled_kinds(conn) == ["bar", "action"]
    assert hub.today["NQZ26"] == (live_bar(0),)


@pytest.mark.anyio
async def test_a_short_store_is_checked_again_at_most_once_a_minute(
    tmp_path, conn, clock, monkeypatch
):
    settings = Settings(data_dir=tmp_path / "empty", private_dir=tmp_path / "private")
    calls = []

    def spy(*args, **kw):
        calls.append(args)
        return load_bars(*args, **kw)

    monkeypatch.setattr(service, "load_bars", spy)
    hub = LiveHub(settings)
    try:
        arm(conn, tmp_path, ALWAYS)
        for seconds in range(0, 60, 5):  # a tick every 5 s
            await hub.tick(T0 + timedelta(seconds=seconds))
        assert len(calls) == 1
        await hub.tick(T0 + timedelta(seconds=60))
        assert len(calls) == 2
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_a_strategy_error_while_warming_up_halts(hub, conn, tmp_path, clock):
    arm(conn, tmp_path, ALWAYS)
    await replay(hub, live_bar(0, close=-1.0))
    clock["t"] = T0 + MINUTE
    await hub.tick(clock["t"])
    s = hub.states["Sim101"]
    assert s.halted.startswith("strategy error while warming up: ValueError: a negative close")
    assert hub.children == {} and journalled_kinds(conn) == ["halt"]


async def only_the_halt(hub, conn, clock):
    """Review of #224: a halted account with no strategy left waits for no warm-up, so only its
    halt shows (no false Feed caution, no "replaying" line), now and on the next bar."""
    [acc] = hub.status()["accounts"]
    assert [c["kind"] for c in acc["conditions"]] == [None], acc["conditions"]
    clock["t"] += MINUTE
    await hub.handle_bar(live_bar(int((clock["t"] - T0) / MINUTE) - 1))
    assert "warming" not in payloads(conn, "bar")[-1]
    [acc] = hub.status()["accounts"]
    assert [c["kind"] for c in acc["conditions"]] == [None]


@pytest.mark.anyio
async def test_a_replay_over_its_budget_on_a_bars_start_shows_only_its_halt(
    hub, conn, tmp_path, clock, monkeypatch
):
    monkeypatch.setattr(service, "WARM_TIMEOUT", 0.3)
    spin = ALWAYS.replace("    if bar.close", "    while True:\n        pass\n    if bar.close")
    arm(conn, tmp_path, spin)
    await replay(hub, live_bar(0))
    clock["t"] = T0 + MINUTE
    await hub.handle_bar(live_bar(1))  # starts it, halts in the warm-up, then takes the bar
    assert hub.states["Sim101"].halted.endswith("halts again")
    [acc] = hub.status()["accounts"]
    assert [c["kind"] for c in acc["conditions"]] == [None]
    assert "warming" not in payloads(conn, "bar")[-1]


@pytest.mark.anyio
async def test_a_strategy_that_fails_to_load_shows_only_its_halt(hub, conn, tmp_path, clock):
    arm(conn, tmp_path, "raise SystemExit('broken strategy')\n" + ALWAYS)
    await replay(hub)
    await hub.tick(T0)
    assert hub.states["Sim101"].halted.startswith("cannot load the strategy:")
    await only_the_halt(hub, conn, clock)


@pytest.mark.anyio
async def test_a_start_that_core_start_halts_shows_only_its_halt(hub, conn, tmp_path, clock):
    a = arm(conn, tmp_path, ALWAYS)
    Path(a.strategy_path).write_text(ALWAYS + "\n# edited after arming\n")
    await replay(hub)
    await hub.tick(T0)
    assert hub.states["Sim101"].halted == "strategy file changed since arming; re-arm"
    [acc] = hub.status()["accounts"]
    assert [c["kind"] for c in acc["conditions"]] == [None]


@pytest.mark.anyio
async def test_a_replay_over_its_time_budget_halts(hub, conn, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(service, "WARM_TIMEOUT", 0.3)  # the hub lock is held meanwhile
    slow = ALWAYS.replace(
        "    if bar.close", "    while bar.close > 0:\n        pass\n    if bar.close"
    )
    arm(conn, tmp_path, slow)
    await replay(hub, live_bar(0))
    clock["t"] = T0 + MINUTE
    await hub.tick(clock["t"])
    assert hub.states["Sim101"].halted == (
        "strategy error while warming up: the replay took over 0.3 s; a re-arm later today"
        " replays more bars and halts again"
    )
    assert hub.children == {}
    await only_the_halt(hub, conn, clock)


@pytest.mark.anyio
async def test_entries_wait_until_todays_replay_is_complete(hub, conn, tmp_path, clock):
    # owner decision 2: the feed down or the replay not done; blocked, "warming up"
    hub.resubscribed()
    arm(conn, tmp_path, ALWAYS)
    await hub.tick(T0)
    [acc] = hub.status()["accounts"]
    assert acc["halt_reason"] == (
        "warming up: today's NQ bars from LSE are not complete yet; no new entries until they are"
    )
    await hub.handle_bar(live_bar(0))  # still replaying: kept for the warm-up, nothing else
    assert journalled_kinds(conn) == []
    hub.feed_live("NQZ26")
    clock["t"] = T0 + timedelta(seconds=65)
    await hub.tick(clock["t"])
    assert hub.states["Sim101"].warming is None
    await hub.handle_bar(live_bar(1))
    assert journalled_kinds(conn) == ["bar", "action"]


@pytest.mark.anyio
async def test_a_store_short_of_the_last_trading_day_blocks_entries_with_an_alert(
    settings, conn, tmp_path, clock
):
    # owner decision 3: never pulled by the hub; the user runs `fenrir data pull`
    bars = settings.data_dir / "bars"
    stale = settings.data_dir.parent / "stale"
    bars.rename(stale)
    hub = LiveHub(settings)
    try:
        arm(conn, tmp_path, ALWAYS)
        await hub.tick(T0)
        await hub.tick(T0 + timedelta(seconds=5))
        message = (
            "warming up: the bar store has no NQ bars up to 2026-10-09, the last trading day"
            " before today; run `fenrir data pull`. No new entries until then"
        )
        assert payloads(conn, "alert") == [{"message": message}]  # once
        await hub.tick(T0 + timedelta(seconds=65))  # checked again: still short, not alerted again
        assert payloads(conn, "alert") == [{"message": message}]
        assert hub.status()["accounts"][0]["halt_reason"] == message
        await hub.handle_bar(live_bar(0))  # exits still run; step does not
        assert journalled_kinds(conn) == ["alert", "bar"]
        assert payloads(conn, "bar")[0]["warming"] is True
        stale.rename(bars)  # pulled; checked again a minute after the last check
        clock["t"] = T0 + timedelta(minutes=2, seconds=5)
        await hub.tick(clock["t"])
        assert hub.states["Sim101"].warming is None
        await hub.handle_bar(live_bar(1))
        assert journalled_kinds(conn)[-2:] == ["bar", "action"]
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_a_stale_store_is_never_read_whole(tmp_path, conn, clock, monkeypatch):
    settings = Settings(data_dir=tmp_path / "stale", private_dir=tmp_path / "private")
    days = [T0 - timedelta(days=d) for d in range(10, 40)]  # up to 2026-10-02
    write_bars(settings.data_dir, "NQ", "NQZ26", [replace(live_bar(0), ts=ts) for ts in days])
    calls = []

    def spy(*args, **kw):
        calls.append(args[3:] + tuple(kw.values()))
        return load_bars(*args, **kw)

    monkeypatch.setattr(service, "load_bars", spy)
    hub = LiveHub(settings)
    try:
        arm(conn, tmp_path, ALWAYS)
        await hub.tick(T0)
        assert "no NQ bars up to 2026-10-09" in hub.states["Sim101"].warming
        assert calls and all(start is not None for start, _ in calls)  # a full read is ~13 s
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_a_reconnects_replay_reaches_no_account_twice(hub, conn, tmp_path, clock):
    arm(conn, tmp_path, REPORT)
    await replay(hub, live_bar(0))
    clock["t"] = T0 + MINUTE
    await hub.tick(clock["t"])
    await hub.handle_bar(live_bar(1))
    assert journalled_kinds(conn) == ["bar", "action"]
    await replay(hub, live_bar(0), live_bar(1), live_bar(2))  # the SDK reconnected
    assert journalled_kinds(conn) == [
        "bar",
        "action",
    ]  # repeats dropped; the missed bar kept, unstepped
    assert hub.today["NQZ26"] == tuple(live_bar(m) for m in range(3))  # each bar once
    await hub.handle_bar(live_bar(2))  # a repeat outside a replay is dropped too
    assert journalled_kinds(conn) == ["bar", "action"]
    clock["t"] = T0 + timedelta(minutes=3)
    await hub.handle_bar(live_bar(3))
    assert payloads(conn, "action")[-1]["order_id"].startswith("2 ")  # stepped once each


@pytest.mark.anyio
async def test_run_bars_replays_from_the_session_open(hub, monkeypatch):
    monkeypatch.setattr(service, "TICK_EVERY", 0)
    seen = {}
    done = asyncio.Event()

    async def fake_stream(settings, contracts, **kw):
        seen.update(kw)
        done.set()
        await asyncio.sleep(3600)
        yield

    task = asyncio.create_task(run_bars(hub, fake_stream))
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        task.cancel()
    assert seen["start"] == datetime(2026, 10, 11, 22, 0, tzinfo=UTC)  # Sunday 18:00 ET
    assert seen["on_auth"] == hub.resubscribed and seen["on_live"] == hub.feed_live


@pytest.mark.anyio
async def test_the_server_starts_the_bar_feed_with_the_first_armed_account(
    hub, conn, tmp_path, monkeypatch
):
    subscribed = []

    async def fake_stream(settings, contracts, **kw):
        subscribed.append(kw["start"])
        await asyncio.sleep(3600)
        yield

    monkeypatch.setattr(service, "stream_bars", fake_stream)
    hub.start()
    await hub.tick(T0)
    assert hub._feed is None  # nothing armed: LSE's one socket per key stays free
    arm(conn, tmp_path, ALWAYS)
    await hub.tick(T0)
    async with asyncio.timeout(10):
        while not subscribed:
            await asyncio.sleep(0.01)
    assert subscribed == [datetime(2026, 10, 11, 22, 0, tzinfo=UTC)]
    assert hub.replaying == {"ES", "NQ"}  # nothing warms up before the replay is over
    assert hub.states["Sim101"].warming.startswith("warming up: today's NQ bars from LSE")
    await hub.stop()
    assert hub._feed is None and hub._tasks == []
    await hub.tick(T0)  # e.g. a late route after stop(): no feed that nothing would cancel
    assert hub._feed is None


class FakeFeed:
    """A stand-in for `stream_bars` (no LSE): each subscription is counted, yields what `bars`
    is given, and records that it was closed (the socket released) in `closed`."""

    def __init__(self, stopped: FeedStopped | None = None, slow_close: bool = False):
        self.subscribed = 0
        self.closing = 0
        self.closed = 0
        self.bars: asyncio.Queue[Bar] = asyncio.Queue()
        self.stopped = stopped
        self.release = asyncio.Event()  # a slow close (the close handshake) ends on set()
        if not slow_close:
            self.release.set()

    async def __call__(self, settings, contracts, **kw):
        self.subscribed += 1
        try:
            if self.stopped is not None:
                raise self.stopped
            while True:
                yield await self.bars.get()
        finally:
            self.closing += 1
            await self.release.wait()
            self.closed += 1


async def until(check, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def second(conn, a: Armed) -> Armed:
    b = replace(a, account="Sim102")
    arming.insert_armed(conn, b)
    return b


async def fed(hub, conn, tmp_path, monkeypatch, feed: FakeFeed) -> Armed:
    """The server started and one account armed: the feed subscribed once. The test ticks."""

    async def no_ticks(hub):
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "run_ticks", no_ticks)
    monkeypatch.setattr(service, "stream_bars", feed)
    hub.start()
    a = arm(conn, tmp_path, ALWAYS)
    await hub.tick(T0)
    await until(lambda: feed.subscribed == 1)
    return a


def feed_stopped(hub, feed: FakeFeed, task: asyncio.Task) -> None:
    assert task.done() and task not in hub._tasks and hub._feed is None
    assert feed.closed == feed.subscribed  # the stream closed (its socket free) before the stop
    status = hub.status()["lse"]
    assert status["connected"] is False and status["error"] is None
    assert hub.lse_retry_at is None and hub.replaying == set() and hub.today == {}


@pytest.mark.anyio
async def test_disarming_the_last_account_stops_the_feed_and_rearming_starts_it(
    hub, conn, tmp_path, monkeypatch
):
    # #251: LSE allows one socket per key; a server with nothing armed must not hold it
    feed = FakeFeed()
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    hub.feed_live("NQZ26")
    feed.bars.put_nowait(live_bar(0))
    await until(lambda: "NQZ26" in hub.today)
    task = hub._feed
    assert (await hub.disarm("Sim101"))["ok"]
    feed_stopped(hub, feed, task)
    arm(conn, tmp_path, ALWAYS)
    await hub.tick(T0)
    await until(lambda: feed.subscribed == 2)  # a fresh subscription, replaying today
    assert hub._feed is not None and not hub._feed.done() and hub._feed in hub._tasks
    assert hub.replaying == {"ES", "NQ"}  # nothing warms up before the replay is over
    assert hub.states["Sim101"].warming.startswith("warming up: today's NQ bars from LSE")


@pytest.mark.anyio
async def test_disarming_one_of_two_accounts_keeps_the_feed(hub, conn, tmp_path, monkeypatch):
    feed = FakeFeed()
    a = await fed(hub, conn, tmp_path, monkeypatch, feed)
    second(conn, a)
    await hub.tick(T0)
    task = hub._feed
    assert (await hub.disarm("Sim101"))["ok"]
    assert hub._feed is task and not task.done() and feed.closed == 0
    assert (await hub.disarm("Sim102"))["ok"]
    feed_stopped(hub, feed, task)


@pytest.mark.anyio
async def test_a_disarm_seen_by_a_bars_sync_stops_the_feed_at_the_next_tick(
    hub, conn, tmp_path, monkeypatch
):
    # the bar's sync runs inside the feed task, under the hub lock: it must not await itself
    feed = FakeFeed()
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    hub.feed_live("NQZ26")
    task = hub._feed
    arming.disarm(conn, "Sim101", T0)  # e.g. another process: no sync of the hub's own
    async with asyncio.timeout(5):
        feed.bars.put_nowait(live_bar(0))
        await until(lambda: "NQZ26" in hub.today)
        assert hub.states == {}  # that sync saw the disarm: no deadlock, the account stopped
        await hub.tick(T0)
    feed_stopped(hub, feed, task)


@pytest.mark.anyio
async def test_the_feed_waiting_for_the_hub_lock_stops(hub, conn, tmp_path, monkeypatch):
    feed = FakeFeed()
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    hub.feed_live("NQZ26")
    task = hub._feed
    arming.disarm(conn, "Sim101", T0)
    async with asyncio.timeout(5):
        async with hub._lock:  # as a tick holds it: the feed's bar waits on it
            feed.bars.put_nowait(live_bar(0))
            await until(lambda: len(hub._lock._waiters or ()) == 1)  # the feed's handle_bar
            await hub._sync(T0)
    feed_stopped(hub, feed, task)


@pytest.mark.anyio
async def test_a_stop_whose_caller_is_cancelled_still_waits_for_the_close(
    hub, conn, tmp_path, monkeypatch
):
    # review of #256: released early, the lock would let a re-arm subscribe a second socket
    feed = FakeFeed(slow_close=True)
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    task = hub._feed
    arming.disarm(conn, "Sim101", T0)
    async with asyncio.timeout(5):
        caller = asyncio.create_task(hub.tick(T0))
        await until(lambda: feed.closing == 1)
        caller.cancel()
        await asyncio.sleep(0.05)
        assert not caller.done() and hub._lock.locked()  # still waiting for the close
        feed.release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
    feed_stopped(hub, feed, task)
    assert not hub._lock.locked()


@pytest.mark.anyio
async def test_a_stop_cancelled_again_and_again_still_waits_for_the_close(
    hub, conn, tmp_path, monkeypatch
):
    # #257: a second cancel, or an anyio cancel scope (it cancels again on every await), must
    # not release the lock either while the old stream closes
    feed = FakeFeed(slow_close=True)
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    task = hub._feed
    arming.disarm(conn, "Sim101", T0)
    scopes: list[anyio.CancelScope] = []

    async def scoped_tick():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await hub.tick(T0)

    async with asyncio.timeout(5):
        twice = asyncio.create_task(hub.tick(T0))
        await until(lambda: feed.closing == 1)
        twice.cancel()
        await asyncio.sleep(0.02)
        twice.cancel()
        await asyncio.sleep(0.05)
        assert not twice.done() and hub._lock.locked()
        feed.release.set()
        with pytest.raises(asyncio.CancelledError):
            await twice
        feed_stopped(hub, feed, task)
        assert not hub._lock.locked()

        hub.start()  # a second run: armed again, then a disarm cancelled by a scope
        a = arm(conn, tmp_path, ALWAYS)
        await hub.tick(T0)
        await until(lambda: feed.subscribed == 2)
        task = hub._feed
        feed.release.clear()
        arming.disarm(conn, a.account, T0)
        scoped = asyncio.create_task(scoped_tick())
        await until(lambda: feed.closing == 2)
        scopes[0].cancel()
        await asyncio.sleep(0.05)
        assert not scoped.done() and hub._lock.locked()
        feed.release.set()
        await scoped  # the scope takes its own cancel
    feed_stopped(hub, feed, task)
    assert not hub._lock.locked()


@pytest.mark.anyio
async def test_a_feed_backing_off_after_feed_stopped_stops(hub, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "FEED_RETRY", 3600)
    feed = FakeFeed(FeedStopped("Max 1 concurrent websocket connection(s)"))
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    await until(lambda: hub.lse_error is not None)  # sleeping before it resubscribes
    task = hub._feed
    async with asyncio.timeout(5):
        assert (await hub.disarm("Sim101"))["ok"]
    feed_stopped(hub, feed, task)


@pytest.mark.anyio
async def test_stop_after_the_feed_stopped_is_clean(hub, conn, tmp_path, monkeypatch, caplog):
    feed = FakeFeed()
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    assert (await hub.disarm("Sim101"))["ok"]
    with caplog.at_level(logging.ERROR):
        await hub.stop()
    assert "live task had failed" not in caplog.text
    assert hub._feed is None and hub._tasks == [] and feed.subscribed == feed.closed == 1


@pytest.mark.anyio
async def test_the_kill_switch_stops_the_feed_only_once_no_row_is_left(
    hub, conn, tmp_path, monkeypatch
):
    feed = FakeFeed()
    await fed(hub, conn, tmp_path, monkeypatch, feed)
    task = hub._feed
    delete = service.delete_armed

    def locked(conn, account):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "delete_armed", locked)
    assert not (await hub.kill())["ok"]  # the row is still armed (halted): it keeps the feed
    await hub.tick(T0)
    assert hub._feed is task and not task.done() and feed.closed == 0
    monkeypatch.setattr(service, "delete_armed", delete)
    assert (await hub.kill())["ok"]
    feed_stopped(hub, feed, task)
