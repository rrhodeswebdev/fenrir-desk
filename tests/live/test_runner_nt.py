"""22.2: NinjaTrader's account figures (DEC-4b), the sim/live mode check and kept peaks (DEC-4a)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from backtest_helpers import D1, D2, bar, make_rules
from live_helpers import decide_now, make_setup

from fenrir.core.actions import Bracket
from fenrir.live import core, journal
from fenrir.live.core import DayStart, Log, Start

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
    assert effects == (Log("peak", {"peak": 50_600.0, "ts": journal.iso(now)}),)
    assert core.on_account(s, account(balance=50_500.0), now)[1] == ()  # not a new high


def test_end_of_day_peak_rises_to_ninjatraders_balance_at_the_session_close():
    s = running(sim(), account(balance=50_400.0))
    assert s.acct.peak == 0.0  # end_of_day: only at the close
    nxt = at("09:30", D2)
    s, effects = core.on_bar(s, nxt)
    assert (s.acct.peak, s.acct.floor, s.session_floor) == (400.0, -1600.0, -1600.0)
    assert peaks(effects) == [{"peak": 50_400.0, "ts": journal.iso(nxt.ts)}]


def test_the_runners_own_peak_raise_is_journalled():
    s, _ = decide_now(running(sim(trailing="intraday")), [LONG])
    entry = {
        "client_id": s.working.client_id,
        "qty": 1,
        "price": 20000.0,
        "commission": 0.0,
        "ts": journal.iso(T.ts + timedelta(minutes=1, seconds=1)),
    }
    s, _ = core.on_fill(s, entry)
    b = at("09:31", hi=20010.0)  # the bar's high: +$200 open
    s, effects = core.on_bar(s, b)
    assert peaks(effects) == [{"peak": 50_200.0, "ts": journal.iso(b.ts)}]


def test_restart_uses_the_highest_of_the_armed_and_journalled_peaks():
    kept = [{"peak": 50_700.0, "ts": "x"}, {"peak": 50_300.0, "ts": "y"}]
    s, effects = core.start(sim(), T.ts, peaks=kept)
    assert (s.acct.peak, s.acct.floor, effects) == (700.0, -1300.0, ())
    higher = make_setup(make_rules(), mode="sim", start=replace(START, peak=50_900.0))
    assert core.start(higher, T.ts, peaks=kept)[0].acct.peak == 900.0


def test_an_end_of_day_close_missed_while_down_halts_until_rearmed():
    before, now = at("16:59").ts, at("09:30", D2).ts  # last journal row on D1, restart on D2
    s, (halt,) = core.start(sim(), now, last_row=before)
    assert halt.kind == "halt" and halt.payload["kind"] is None
    assert s.halted.startswith("confirm balances: ") and s.halt_day is None and s.started
    _, effects = decide_now(core.on_bar(s, at("09:30", D2))[0], [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    # restarted again: restored from the journal (#169), nothing journalled twice
    s, effects = core.start(sim(), now + timedelta(days=1), (halt.payload,), last_row=now)
    assert effects == () and s.halted.startswith("confirm balances: ")
    # no journal row since arming: the arming time counts as the last time seen
    armed_d1 = sim()
    armed_d1 = replace(armed_d1, armed=replace(armed_d1.armed, armed_at=before))
    assert core.start(armed_d1, now)[0].halted.startswith("confirm balances: ")
    # re-armed with the confirmed peak: a fresh armed_at, so the old halt row is not passed
    rearmed = replace(armed_d1, armed=replace(armed_d1.armed, armed_at=now))
    assert core.start(rearmed, now + timedelta(minutes=1))[0].halted is None


@pytest.mark.parametrize(
    "setup",
    [
        sim(trailing="intraday"),  # the peak follows each account message, not the close
        make_setup(make_rules()),  # dry runs start fresh
    ],
)
def test_no_confirm_balances_halt_unless_end_of_day_sim_or_live(setup):
    assert core.start(setup, at("09:30", D2).ts, last_row=at("16:59").ts)[0].halted is None


def test_no_confirm_balances_halt_when_the_runner_saw_the_close():
    seen = at("18:05").ts  # D2's trading day: the roll ran
    assert core.start(sim(), at("09:30", D2).ts, last_row=seen)[0].halted is None
