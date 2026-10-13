from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from backtest_helpers import D1, D2, D3, bar, make_rules
from live_helpers import decide_now, make_setup

from fenrir.core.actions import Bracket
from fenrir.live import core
from fenrir.live.core import DayStart, Start

LONG = Bracket("long", 1, 19990.0, 20020.0)


def at(hhmm, day=D1):
    return bar(hhmm, 20000.0, 20005.0, 19995.0, 20000.0, day)


T0 = at("09:30").ts


def started(setup):
    return core.start(setup, T0)[0]


def kinds(effects):
    return [e.kind for e in effects]


def with_balance(s, balance):
    return replace(s, acct=replace(s.acct, balance=balance))


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 12, 13, 30, tzinfo=UTC), True),  # Mon 09:30 ET
        (datetime(2026, 10, 12, 21, 30, tzinfo=UTC), False),  # Mon 17:30 ET daily break
        (datetime(2026, 10, 12, 22, 0, tzinfo=UTC), True),  # Mon 18:00 ET reopen
        (datetime(2026, 10, 16, 21, 0, tzinfo=UTC), False),  # Fri 17:00 ET close
        (datetime(2026, 10, 17, 15, 0, tzinfo=UTC), False),  # Saturday
        (datetime(2026, 10, 18, 21, 59, tzinfo=UTC), False),  # Sun 17:59 ET
        (datetime(2026, 10, 18, 22, 0, tzinfo=UTC), True),  # Sun 18:00 ET open
    ],
)
def test_in_session(utc, expected):
    assert core.in_session(utc) is expected


def test_stale_data_pauses_then_resumes():
    s, _ = core.on_bar(started(make_setup(make_rules())), at("09:30"))
    closed = at("09:31").ts  # the 09:30 bar's close
    s, effects = core.on_tick(s, closed + timedelta(seconds=90), 90)
    assert effects == () and s.stale is None
    s, effects = core.on_tick(s, closed + timedelta(seconds=91), 90)
    assert kinds(effects) == ["alert"] and "no NQ bar for 91s" in effects[0].payload["message"]
    st = core.account_status(s)
    assert (st["halted"], st["halt_kind"]) == (True, "stale")
    assert decide_now(s, [LONG])[1][-1].payload["reason"].startswith("paused: no NQ bar")
    s, effects = core.on_tick(s, closed + timedelta(seconds=120), 90)
    assert effects == ()  # one alert, not one per tick
    s, _ = core.on_bar(s, at("09:32"))
    assert kinds(decide_now(s, [LONG])[1]) == ["bar", "action"]  # cleared by the bar
    assert core.account_status(s)["halted"] is False
    # the bar's close starts the next stale window
    assert core.on_tick(s, at("09:33").ts + timedelta(seconds=90), 90)[1] == ()


def test_no_stale_alert_outside_session():
    s = started(make_setup(make_rules()))
    saturday = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
    s, effects = core.on_tick(replace(s, last_seen=saturday - timedelta(hours=20)), saturday, 90)
    assert effects == () and s.stale is None
    # nor in the first minutes after the Sunday open, before a bar could arrive
    sunday = datetime(2026, 10, 18, 22, 2, tzinfo=UTC)
    assert core.on_tick(replace(s, last_seen=saturday), sunday, 90)[1] == ()


def test_stale_pause_keeps_a_day_halt():
    s = started(make_setup(make_rules(daily_loss=500.0)))
    s, _ = core.on_bar(with_balance(s, -400.0), at("09:30"))
    s, _ = core.on_tick(s, s.last_seen + timedelta(seconds=91), 90)
    s, _ = core.on_bar(s, at("09:32"))  # clears the pause, not the safety halt
    assert (s.stale, s.halt_kind) == (None, "safety")
    assert core.account_status(s)["halt_kind"] == "safety"


def test_day_halt_lifts_next_trading_day():
    s = started(make_setup(make_rules(daily_loss=500.0)))
    s, effects = core.on_bar(with_balance(s, -400.0), at("09:30"))
    assert kinds(effects) == ["halt"] and (s.halt_kind, s.halt_day) == ("safety", D1)
    s, effects = core.on_bar(s, at("10:30"))
    assert effects == () and s.halted.startswith("safety margin")
    s, effects = core.on_bar(s, at("09:30", D2))
    assert kinds(effects) == ["alert"] and effects[0].payload["message"].startswith("halt lifted")
    assert (s.halted, s.halt_kind, s.halt_day, s.acct.day) == (None, None, None, D2)
    assert kinds(decide_now(s, [LONG])[1]) == ["bar", "action"]
    # the safety check runs again on the new day, against the new day's level
    assert core.safety_level(s) == -800.0  # opened at −400: −400 − 0.8 × 500
    s, effects = core.on_bar(with_balance(s, -800.0), at("09:31", D2))
    assert kinds(effects) == ["halt"] and (s.halt_kind, s.halt_day) == ("safety", D2)


def test_breach_halt_survives_day_roll():
    s, _ = core.halt(started(make_setup(make_rules())), "breach max_drawdown: x")
    s, effects = core.on_bar(s, at("09:30", D2))
    assert effects == () and s.halted == "breach max_drawdown: x"


def test_breach_at_day_close_replaces_a_safety_halt():
    s = started(make_setup(make_rules()))  # no DLL: safety level −1600, floor −2000
    s, effects = core.on_bar(with_balance(s, -1700.0), at("09:30"))
    assert kinds(effects) == ["halt"] and s.halt_kind == "safety"
    s, effects = core.on_bar(with_balance(s, -2000.0), at("09:30", D2))
    assert kinds(effects) == ["halt"]  # no "halt lifted" alert
    assert effects[0].payload == {
        "reason": "breach max_drawdown: equity -2000.00 at or below drawdown floor -2000.00",
        "kind": "breach",
        "until": None,
    }
    assert (s.halt_kind, s.halt_day) == ("breach", None)
    s, effects = core.on_bar(s, at("09:30", D3))
    assert effects == () and s.halt_kind == "breach"


BREACH = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}
ERROR = {"reason": "strategy error: boom", "kind": None, "until": None}
SAFETY = {"reason": "safety margin: x", "kind": "safety", "until": D1.isoformat()}


def test_restart_restores_a_breach_until_disarmed():
    s, effects = core.start(make_setup(make_rules()), T0, (BREACH,))
    assert effects == ()  # already journalled
    assert (s.halted, s.halt_kind, s.halt_day) == ("breach max_drawdown: x", "breach", None)
    s, _ = core.on_bar(s, at("09:30"))
    assert decide_now(s, [LONG])[1][-1].payload["reason"] == "halted: breach max_drawdown: x"
    s, effects = core.on_bar(s, at("09:30", D2))
    assert effects == () and s.halt_kind == "breach"


def test_restart_restores_a_day_halt_on_its_own_day_only():
    s, _ = core.start(make_setup(make_rules()), T0, (SAFETY,))
    assert (s.halted, s.halt_kind, s.halt_day) == ("safety margin: x", "safety", D1)
    s, effects = core.on_bar(s, at("09:30", D2))
    assert kinds(effects) == ["alert"] and s.halted is None  # lifts as without a restart
    s, _ = core.start(make_setup(make_rules()), at("09:30", D2).ts, (SAFETY,))
    assert s.halted is None


def test_restart_keeps_the_breach_whatever_the_journal_order():
    for halts in [(BREACH, ERROR, SAFETY), (ERROR, BREACH), (SAFETY, BREACH)]:
        s, _ = core.start(make_setup(make_rules()), T0, halts)
        assert (s.halted, s.halt_kind) == ("breach max_drawdown: x", "breach")
    s, _ = core.start(make_setup(make_rules()), T0, (ERROR, SAFETY))
    assert (s.halted, s.halt_day) == ("strategy error: boom", None)  # never downgraded


def test_breach_over_an_open_ended_halt_is_journalled_and_alerted():
    s, _ = core.halt(started(make_setup(make_rules())), "strategy error: boom")
    s, effects = core.on_bar(with_balance(s, -2000.0), at("09:30", D2))
    assert kinds(effects) == ["halt", "alert"]
    assert (effects[0].payload["kind"], effects[0].payload["until"]) == ("breach", None)
    assert "strategy error: boom" in effects[1].payload["message"]
    assert (s.halt_kind, s.halt_day) == ("breach", None)
    assert core.account_status(s)["halt_kind"] == "breach"


def test_a_breach_never_replaces_a_breach():
    s = started(make_setup(make_rules()))
    s, effects = core.on_bar(with_balance(s, -2000.0), at("09:30", D2))
    first = s.halted
    assert kinds(effects) == ["halt"] and s.halt_kind == "breach"
    s, effects = core.on_bar(with_balance(s, -2500.0), at("09:31", D2))  # breaches again
    assert effects == () and s.halted == first
    s, effects = core.on_bar(s, at("09:30", D3))
    assert effects == () and s.halted == first


def test_day_roll_resets_the_session_from_the_close():
    # sim, end-of-day trailing: opened at +1000 (peak +1000, floor −1000), +1500 now
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=51_500.0)
    day = DayStart(balance=51_000.0, peak=51_000.0)
    s = started(make_setup(make_rules(), mode="sim", start=start, day_start=day))
    s, _ = core.on_bar(s, at("09:30"))
    assert (s.session_start, s.session_floor) == (1000.0, -1000.0)
    s, _ = core.on_bar(s, at("18:00"))  # first bar of D2
    # the close trailed the floor to +1500 − 2000, and the morning's P&L is a closed day
    assert (s.session_start, s.session_floor, s.acct.floor) == (1500.0, -500.0, -500.0)
    assert s.acct.closed_days == ((D1, 500.0),)
    assert core.account_status(s)["today_pnl"] == 0.0
    assert core.account_status(s)["session"]["equity"] == [[at("18:00").ts.isoformat(), 1500.0]]


def test_restart_without_a_trade_since_the_open_books_no_day():
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=51_000.0)
    day = DayStart(balance=51_000.0, peak=51_000.0)
    s = started(make_setup(make_rules(), mode="sim", start=start, day_start=day))
    s, _ = core.on_bar(s, at("09:30", D2))
    assert s.acct.closed_days == ()


def test_intraday_session_floor_trails_at_least_the_open_balance():
    # NinjaTrader's open peak below its open balance: the intraday peak is at least the balance
    start = Start(phase_start=50_000.0, peak=50_500.0, balance=50_500.0)
    day = DayStart(balance=50_500.0, peak=50_000.0)
    rules = make_rules(trailing="intraday")
    s = started(make_setup(rules, mode="sim", start=start, day_start=day))
    assert (s.session_start, s.session_floor) == (500.0, -1500.0)


def test_a_stale_pause_is_reported_under_a_halt():
    # #212: a halt shows as halt_kind, but the stale pause must still light Feed
    s = started(make_setup(make_rules(daily_loss=500.0)))
    s, _ = core.on_bar(with_balance(s, -400.0), at("09:30"))
    s, _ = core.on_tick(s, s.last_seen + timedelta(seconds=91), 90)
    st = core.account_status(s)
    assert (st["halted"], st["halt_kind"]) == (True, "safety")
    assert [c["kind"] for c in st["conditions"]] == ["safety", "stale"]
    assert st["conditions"][1]["reason"].startswith("no NQ bar for 91s")
    s, _ = core.on_bar(s, at("09:32"))  # the bar clears the pause, not the halt
    assert [c["kind"] for c in core.account_status(s)["conditions"]] == ["safety"]


def test_conditions_list_every_pause_in_the_order_halt_kind_picks():
    s = started(make_setup(make_rules()))
    s = replace(s, halted="strategy error: x", mismatch="m", stale="st", drift="d")
    st = core.account_status(s)
    assert st["conditions"] == [
        {"kind": None, "reason": "strategy error: x"},  # a halt no cell names
        {"kind": "reconcile", "reason": "m"},
        {"kind": "stale", "reason": "st"},
        {"kind": "drift", "reason": "d"},
    ]
    assert (st["halted"], st["halt_kind"], st["halt_reason"]) == (True, None, "strategy error: x")
    calm = core.account_status(replace(s, halted=None, mismatch=None, stale=None, drift=None))
    assert (calm["halted"], calm["conditions"]) == (False, [])
