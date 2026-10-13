"""22.2: NinjaTrader's account figures (DEC-4b), the sim/live mode check and kept peaks (DEC-4a)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, bar, make_rules, rules_data
from live_helpers import decide_now, make_setup

from fenrir.core.actions import Bracket
from fenrir.live import core
from fenrir.live.core import DayStart, Log, Start
from fenrir.live.journal import iso
from fenrir.rules.schema import AccountRules

LONG = Bracket("long", 1, 19990.0, 20020.0)  # stop-out: (10 + 1 tick) x $20 = $205
START = Start(phase_start=50_000.0, peak=50_000.0, balance=50_000.0)
OPEN = DayStart(balance=50_000.0, peak=50_000.0)


def at(hhmm, day=D1, hi=20005.0):
    return bar(hhmm, 20000.0, hi, 19995.0, 20000.0, day)


T = at("09:30")


def sim(mode="sim", **kw):
    return make_setup(make_rules(**kw), mode=mode, start=START, day_start=OPEN)


def account(balance=50_000.0, today=0.0, unrealized=0.0, is_sim=True):
    """An add-on "account" stream message (validated by fenrir.bridge.client), absolute $."""
    return {
        "type": "account",
        "id": "Sim101",
        "name": "Sim101",
        "is_sim": is_sim,
        "balance": balance,
        "realized_pnl_today": today,
        "unrealized_pnl": unrealized,
    }


def running(setup, *messages, b=T):
    """Started, given NinjaTrader's `messages`, then fed bar `b`."""
    s = core.start(setup, b.ts)[0]
    for m in messages:
        s = core.on_account(s, m, b.ts)[0]
    return core.on_bar(s, b)[0]


def kinds(effects):
    return [e.kind if isinstance(e, Log) else "send" for e in effects]


def peaks(effects):
    return [e.payload for e in effects if isinstance(e, Log) and e.kind == "peak"]


# DEC-4b: the stricter today's P&L drives the safety margin and the pre-trade check


def test_ninjatraders_worse_today_pnl_halts_at_the_safety_margin():
    setup = sim(daily_loss=1000.0)
    assert running(setup).halted is None  # the app's own figures: flat, $0 today
    s = running(setup, account(today=-850.0))  # 80% of the $1000 DLL is -800
    assert s.halt_kind == "safety" and s.halt_day == D1
    assert s.halted == "safety margin: equity -850.00 at or below -800.00"


def test_ninjatraders_worse_today_pnl_rejects_the_bracket():
    two = replace(LONG, qty=2)  # stop-out $410
    _, effects = decide_now(running(sim(daily_loss=1000.0)), [two])
    assert kinds(effects) == ["bar", "action", "send"]
    s = running(sim(daily_loss=1000.0), account(today=-700.0))
    _, effects = decide_now(s, [two])
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["reason"] == (
        "stop-out loss 410.00 would put day P&L -1110.00 at or below daily loss limit -1000.00"
    )
    st = core.account_status(s)
    assert (st["today_pnl"], st["limits"]["daily_loss_remaining"]) == (-700.0, 300.0)


# DEC-4b: drawdown remaining is also checked against NinjaTrader's balance, on the same floor


def test_ninjatraders_lower_balance_is_checked_against_the_drawdown_floor():
    setup = replace(sim(), safety_margin_pct=100.0)  # level = floor: the pre-trade check alone
    s = running(setup, account(balance=48_200.0))  # -1800 from phase start; app equity 0
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["reason"] == (
        "stop-out loss 205.00 would put equity -2005.00 at or below drawdown floor -2000.00"
    )
    st = core.account_status(s)
    # balance is the app's, relative to phase start; nt_balance is NinjaTrader's, absolute
    assert (st["balance"], st["nt_balance"]) == (0.0, 48_200.0)
    assert st["limits"]["drawdown_remaining"] == 200.0


def test_ninjatraders_lower_equity_halts_at_the_safety_margin():
    # floor -2000 + 20% of the 2000 room at the open = -1600
    s = running(sim(), account(balance=48_500.0, unrealized=-150.0))
    assert s.halt_kind == "safety"
    assert s.halted == "safety margin: equity -1650.00 at or below -1600.00"


# a sim/live mode mismatch with the NinjaTrader account halts


@pytest.mark.parametrize(("mode", "is_sim"), [("sim", False), ("live", True)])
def test_mode_mismatch_halts_until_disarmed(mode, is_sim):
    s = core.start(sim(mode=mode), T.ts)[0]
    s, effects = core.on_account(s, account(is_sim=is_sim), T.ts)
    assert kinds(effects) == ["halt"]
    assert s.halted.startswith(f"mode mismatch: armed {mode}, NinjaTrader reports ")
    assert (s.halt_kind, s.halt_day) == (None, None)
    assert core.on_account(s, account(is_sim=is_sim), T.ts)[1] == ()  # journalled once
    _, effects = decide_now(core.on_bar(s, T)[0], [LONG])
    assert kinds(effects) == ["bar", "rejection"]


def test_matching_mode_does_not_halt_and_dry_runs_ignore_ninjatrader():
    assert core.on_account(core.start(sim(), T.ts)[0], account(), T.ts)[0].halted is None
    live = core.start(sim(mode="live"), T.ts)[0]
    assert core.on_account(live, account(is_sim=False), T.ts)[0].halted is None
    dry = core.start(make_setup(make_rules()), T.ts)[0]
    s, effects = core.on_account(dry, account(balance=1.0, is_sim=False), T.ts)
    assert (s, effects) == (dry, ())


# DEC-4a: the peak follows NinjaTrader, each raise is journalled, and a restart keeps it


def test_intraday_peak_follows_ninjatraders_equity():
    now = T.ts + timedelta(seconds=30)
    s = core.start(sim(trailing="intraday"), T.ts)[0]
    s, effects = core.on_account(s, account(balance=50_400.0, unrealized=200.0), now)
    assert (s.acct.peak, s.acct.floor) == (600.0, -1400.0)
    assert effects == (Log("peak", {"peak": 50_600.0, "ts": iso(now)}),)
    assert core.on_account(s, account(balance=50_500.0), now)[1] == ()  # not a new high


def test_end_of_day_peak_rises_to_ninjatraders_balance_at_the_session_close():
    s = running(sim(), account(balance=50_400.0))
    assert s.acct.peak == 0.0  # end_of_day: only at the close
    nxt = at("09:30", D2)
    s, effects = core.on_bar(s, nxt)
    assert (s.acct.peak, s.acct.floor, s.session_floor) == (400.0, -1600.0, -1600.0)
    assert peaks(effects) == [{"peak": 50_400.0, "ts": iso(nxt.ts)}]


def test_the_runners_own_peak_raise_is_journalled():
    s, _ = decide_now(running(sim(trailing="intraday")), [LONG])
    entry = {
        "client_id": s.working.client_id,
        "qty": 1,
        "price": 20000.0,
        "commission": 0.0,
        "ts": iso(T.ts + timedelta(minutes=1, seconds=1)),
    }
    s, _ = core.on_fill(s, entry)
    b = at("09:31", hi=20010.0)  # the bar's high: +$200 open
    s, effects = core.on_bar(s, b)
    assert peaks(effects) == [{"peak": 50_200.0, "ts": iso(b.ts)}]


def fill_at(ts, qty=0):
    """An add-on "fill" message (validated by fenrir.bridge.client) that is not Fenrir's."""
    return {"client_id": "other", "qty": qty, "price": 20000.0, "commission": 0.0, "ts": iso(ts)}


@pytest.mark.parametrize("qty", [0, 1])
def test_a_fill_that_rolls_the_day_journals_the_close_peak(qty):
    s = running(sim(), account(balance=50_400.0))
    ts = at("09:30", D2).ts + timedelta(seconds=5)
    s, effects = core.on_fill(s, fill_at(ts, qty))
    assert s.acct.peak == 400.0
    assert peaks(effects) == [{"peak": 50_400.0, "ts": iso(ts)}]


def test_every_end_of_day_roll_journals_the_peak_once_ninjatrader_is_known():
    nxt = at("09:30", D2)
    _, effects = core.on_bar(running(sim(), account(balance=49_000.0)), nxt)  # not a new high
    assert peaks(effects) == [{"peak": 50_000.0, "ts": iso(nxt.ts)}]
    s, effects = core.on_account(running(sim()), account(balance=50_400.0), T.ts)
    assert effects == ()  # end_of_day: no row between rolls, so a row means a roll
    assert peaks(core.on_bar(running(sim()), nxt)[1]) == []  # no NinjaTrader figures: no proof


def test_the_ninjatrader_peak_respects_locks_at():
    d = rules_data(trailing="intraday")
    d["evaluation"]["max_drawdown"]["locks_at"] = 2100.0
    setup = make_setup(AccountRules.model_validate(d), mode="sim", start=START, day_start=OPEN)
    s, _ = core.on_account(core.start(setup, T.ts)[0], account(balance=52_600.0), T.ts)
    assert (s.acct.peak, s.acct.floor) == (2600.0, 100.0)  # min(2600, 2100) - 2000


def test_the_intraday_start_raise_to_the_balance_is_journalled():
    setup = make_setup(
        make_rules(trailing="intraday"), mode="sim", start=replace(START, balance=50_300.0)
    )
    s, effects = core.start(setup, T.ts)
    assert s.acct.peak == 300.0 and effects == (Log("peak", {"peak": 50_300.0, "ts": iso(T.ts)}),)
    assert core.start(setup, T.ts, peaks=[effects[0].payload])[1] == ()  # kept: not again


def test_restart_uses_the_highest_of_the_armed_and_journalled_peaks():
    kept = [{"peak": 50_700.0, "ts": iso(T.ts)}, {"peak": 50_300.0, "ts": iso(T.ts)}]
    s, effects = core.start(sim(), T.ts, peaks=kept)
    assert (s.acct.peak, s.acct.floor, effects) == (700.0, -1300.0, ())
    higher = make_setup(make_rules(), mode="sim", start=replace(START, peak=50_900.0))
    assert core.start(higher, T.ts, peaks=kept)[0].acct.peak == 900.0


# DEC-4a: an end_of_day close missed while down halts with "confirm balances"

FRI = date(2025, 10, 10)


def armed_at(setup, ts):
    return replace(setup, armed=replace(setup.armed, armed_at=ts))


def rolled(ts):
    """The "peak" row a day roll at `ts` journals."""
    return {"peak": 50_000.0, "ts": iso(ts)}


def test_a_close_missed_while_down_halts_until_rearmed():
    setup = armed_at(sim(), at("08:00").ts)
    now = at("09:30", D2).ts  # D1's close and D2's open passed with no roll journalled
    s, (halt,) = core.start(setup, now, peaks=[rolled(at("09:00").ts)])
    assert halt.kind == "halt" and halt.payload["kind"] is None
    assert s.halted.startswith("confirm balances: ") and s.halt_day is None and s.started
    _, effects = decide_now(core.on_bar(s, at("09:30", D2))[0], [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    # restarted again: restored from the journal (#169), nothing journalled twice
    s, effects = core.start(setup, now + timedelta(days=1), (halt.payload,))
    assert effects == () and s.halted.startswith("confirm balances: ")
    # no roll journalled since arming: the arming counts as the last one
    assert core.start(setup, now)[0].halted.startswith("confirm balances: ")
    assert core.start(setup, at("17:30").ts)[0].halted is None  # D1's break: no session since
    # re-armed with the confirmed peak: a fresh armed_at, so the old halt row is not passed
    assert core.start(armed_at(setup, now), now + timedelta(minutes=1))[0].halted is None


def test_a_roll_journalled_since_the_close_is_proof():
    setup = armed_at(sim(), at("08:00").ts)
    roll = at("18:05").ts  # D2's first bar rolled D1
    assert core.start(setup, at("09:30", D2).ts, peaks=[rolled(roll)])[0].halted is None


def test_a_weekend_restart_asks_only_once_the_next_session_opened():
    setup = armed_at(sim(), at("08:00").ts)
    last = [rolled(at("09:30", FRI).ts)]  # Thursday evening's roll into Friday
    for now in (at("17:30", FRI), at("12:00", date(2025, 10, 11)), at("17:59", date(2025, 10, 12))):
        # Friday's close is taken at the Sunday 18:00 roll: nothing traded since
        assert core.start(setup, now.ts, peaks=last)[0].halted is None
    sunday_open = at("18:05", date(2025, 10, 12)).ts  # Monday's session has opened
    assert core.start(setup, sunday_open, peaks=last)[0].halted.startswith("confirm balances: ")


@pytest.mark.parametrize(
    "setup",
    [
        sim(trailing="intraday"),  # the peak follows each account message, not the close
        make_setup(make_rules()),  # dry runs start fresh
    ],
)
def test_no_confirm_balances_halt_unless_end_of_day_sim_or_live(setup):
    setup = armed_at(setup, at("08:00").ts)
    assert core.start(setup, at("09:30", D2).ts)[0].halted is None
