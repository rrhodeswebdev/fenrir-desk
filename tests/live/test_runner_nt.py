"""22.2: NinjaTrader's account figures (DEC-4b), the sim/live mode check and kept peaks (DEC-4a)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, bar, make_rules, rules_data
from live_helpers import INSTRUMENT, decide_now, make_setup, reconciled

from fenrir.bridge.reconcile import NtOrder, NtPosition
from fenrir.core.actions import Bracket, Flatten
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


def running(setup, *messages, b=T, state=None):
    """Started (or `state`), given NinjaTrader's `messages`, then fed bar `b`."""
    s = reconciled(state or core.start(setup, b.ts)[0])
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


def test_the_end_of_day_peak_rises_to_the_balance_not_the_equity():
    """DEC-4a: NinjaTrader's balance at the close, never its open P&L (review of #221, X4)."""
    s = running(sim(), account(balance=50_400.0, unrealized=300.0))
    s, _ = core.on_bar(s, at("09:30", D2))
    assert (s.acct.peak, s.acct.floor) == (400.0, -1600.0)


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
    thursday = [rolled(at("09:30", date(2025, 10, 9)).ts)]  # Wednesday evening's roll
    friday_open = at("18:05", date(2025, 10, 9)).ts  # Friday is a session day too
    assert core.start(setup, friday_open, peaks=thursday)[0].halted.startswith("confirm balances")


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


# A roll needs NinjaTrader's balance at the close: without it (none yet, or lost with the link)
# that close is never taken, so the running account halts as a restart would


def test_a_roll_without_ninjatraders_figures_halts_confirm_balances():
    s, effects = core.on_bar(running(sim()), at("09:30", D2))
    assert s.halted.startswith("confirm balances: ") and s.halt_kind is None
    assert "no NinjaTrader balance at the" in s.halted
    assert [e.payload["kind"] for e in effects if e.kind == "halt"] == [None]
    assert peaks(effects) == []


def test_a_lost_link_leaves_no_figures_for_the_roll():
    s = running(sim(), account(balance=50_400.0))
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    assert s.nt is None  # stale from here on: never a close's balance
    assert core.on_bar(s, at("09:30", D2))[0].halted.startswith("confirm balances: ")
    s, _ = core.on_reconcile(s, "NQ 12-25", (), (), T.ts)
    s, _ = core.on_account(s, account(balance=50_400.0), T.ts)  # fresh figures again
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted is None and peaks(effects)[0]["peak"] == 50_400.0


@pytest.mark.parametrize(
    "setup",
    [
        sim(trailing="intraday"),  # the peak follows each account message, not the close
        make_setup(make_rules(), start=START),  # dry runs have no NinjaTrader figures
    ],
)
def test_only_end_of_day_sim_or_live_rolls_need_ninjatraders_figures(setup):
    assert core.on_bar(running(setup), at("09:30", D2))[0].halted is None


def test_a_second_outage_drops_the_figures_too():
    """Reconnected, the reconcile read failed (still LINK_LOST), figures flowed, link lost again."""
    s = running(sim(), account(balance=50_400.0))
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    s, _ = core.on_account(s, account(balance=50_400.0), T.ts)
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")
    assert effects == () and s.nt is None  # one alert per outage, but never stale figures
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith("confirm balances: ") and peaks(effects) == []


# #211: today's open (day_start) comes from the journal, else from NinjaTrader's figures at start


def first_start(balance=50_300.0, realized=0.0, opens=(), now=T.ts):
    """A sim start from the first account message (`balance`, `realized` today), with no
    day_start given and the journalled opens since arming."""
    setup = replace(sim(), start=replace(START, balance=balance, realized_today=realized))
    return core.start(replace(setup, day_start=None), now, opens=opens)


def opened(day, balance, peak=50_000.0):
    """A "day_start" payload journalled for trading day `day`."""
    return {"day": day.isoformat(), "balance": balance, "peak": peak, "ts": iso(T.ts)}


@pytest.mark.parametrize("realized", [0.0, -800.0, -1000.0])
def test_without_a_journalled_open_ninjatraders_own_open_opens_the_day(realized):
    """The open is NinjaTrader's balance less its realized loss today, so a morning loss counts
    once in the stricter-of equity (DEC-4b), not twice: the room and the safety margin are right."""
    yesterday = opened(D1 - timedelta(days=1), 1.0)
    balance = 50_000.0 + realized
    s, effects = first_start(balance, realized, opens=[yesterday])
    assert s.setup.day_start == DayStart(50_000.0, 50_000.0)
    assert (s.acct.balance, s.acct.day_start_balance, s.session_start) == (realized, 0.0, 0.0)
    assert s.acct.traded_today is (realized != 0.0)
    note, row = effects
    assert note.kind == "alert" and "realized loss today, 50000.00" in note.payload["message"]
    assert row == Log("day_start", opened(D1, 50_000.0))
    s = running(s.setup, account(balance, today=realized), state=s)
    st = core.account_status(s)
    assert (st["today_pnl"], st["limits"]["drawdown_remaining"]) == (realized, 2000.0 + realized)
    assert s.halted is None  # L=1000 left the safety margin's room: no false halt
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "action", "send"]  # today's open is known


def test_a_morning_gain_opens_the_day_at_the_balance_and_stays_in_it():
    """A realized gain gives no extra daily room (stricter than the firm by the gain, and
    whatever time NinjaTrader resets its realized P&L), and a replay never cuts it from the
    balance: no journalled fill holds it (review of #217 at 33bd517)."""
    s, effects = first_start(50_500.0, 500.0)
    assert s.setup.day_start == DayStart(50_500.0, 50_000.0)
    assert kinds(effects) == ["alert", "day_start"]
    s = core.replay(s, [])
    assert (s.acct.balance, s.acct.day_start_balance, s.session_start) == (500.0, 500.0, 500.0)
    s = running(s.setup, account(50_500.0, today=500.0), state=s)
    st = core.account_status(s)
    assert (st["balance"], st["today_pnl"], st["limits"]["drawdown_remaining"]) == (
        500.0,
        0.0,
        2500.0,
    )


def test_a_journalled_open_for_today_is_todays_open_and_journals_nothing():
    early, today = opened(D1, 50_100.0), opened(D1, 50_300.0, peak=50_400.0)
    s, effects = first_start(
        49_900.0, -100.0, opens=[opened(D1 - timedelta(days=1), 1.0), early, today]
    )
    assert effects == () and s.setup.day_start == DayStart(50_300.0, 50_400.0)  # the latest
    assert (s.acct.balance, s.acct.day_start_balance) == (-100.0, 300.0)
    assert (s.session_start, s.session_floor) == (300.0, -1600.0)  # the open's floor, peak +400
    assert core.account_status(s)["today_pnl"] == -400.0  # not 0: the restart is mid-day


@pytest.mark.parametrize(
    "setup",
    [
        make_setup(make_rules(), start=replace(START, realized_today=0.0)),  # dry: fresh
        replace(sim(), start=replace(START, realized_today=0.0)),  # day_start given
        replace(sim(), day_start=None),  # no NinjaTrader realized P&L today: no open known
    ],
)
def test_no_open_is_derived_without_the_figures_or_the_need(setup):
    s, effects = core.start(setup, T.ts)
    assert s.setup.day_start == (None if setup.armed.mode == "dry" else setup.day_start)
    assert effects == ()


def test_a_refused_start_journals_no_open():
    setup = replace(sim(), start=replace(START, realized_today=0.0), day_start=None)
    s, effects = core.start(replace(setup, rules_hash="edited"), T.ts)
    assert kinds(effects) == ["halt"] and not s.started


def test_replayed_fills_count_once_against_ninjatraders_open():
    """No open journalled (its row lost), today's +$200 trade journalled: a gain opens the day at
    NinjaTrader's balance, which holds that trade, so the replay never books it twice (the open
    fails closed: the gain gives no extra daily room)."""
    b, rows = T, []
    s, effects = decide_now(running(sim()), [LONG])
    rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    for f in (
        {**fill_at(b.ts + timedelta(seconds=61), 1), "client_id": s.working.client_id},
        {**fill_at(at("09:32").ts, -1), "price": 20010.0},
    ):
        s, effects = core.on_fill(s, f)
        rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    booked = s.acct.balance
    r, _ = first_start(50_000.0 + booked, booked, now=at("09:40").ts)
    r = core.replay(r, rows)
    assert booked == 200.0 and (r.acct.balance, r.acct.day_start_balance) == (booked, booked)


def test_a_first_balance_at_or_below_the_liquidation_level_cannot_start():
    """Never a breach from a figure at start (NinjaTrader's figures never raise one): halted
    "confirm balances" until re-armed, with no figures and no open journalled."""
    s, effects = first_start(48_000.0, -2000.0)  # the pins' floor: 50,000 - 2,000
    assert kinds(effects) == ["halt"] and not s.started and s.halt_kind is None
    assert s.halted == (
        "confirm balances: NinjaTrader's balance 48000.00 is at or below the liquidation level"
        " 48000.00 from the armed balances; re-arm with the confirmed balances"
    )
    s, effects = core.on_bar(core.on_account(s, account(48_000.0), T.ts)[0], T)
    assert s.halt_kind is None and "halt" not in kinds(effects)  # never a breach
    assert core.account_status(s)["balance"] is None
    assert (s.setup.start, s.setup.day_start, s.nt) == (None, None, None)
    s, effects = core.on_bar(s, at("09:30", D2))  # a roll: nothing to raise or open
    assert not {"peak", "day_start", "halt"} & set(kinds(effects))
    assert first_start(48_000.01, -1999.99)[0].started


def test_every_sim_roll_journals_the_new_days_open_and_a_restart_takes_it():
    s = running(sim(), account(balance=50_400.0))
    nxt = at("09:30", D2)
    s, effects = core.on_bar(s, nxt)
    [row] = [e.payload for e in effects if isinstance(e, Log) and e.kind == "day_start"]
    assert row == {"day": D2.isoformat(), "balance": 50_000.0, "peak": 50_400.0, "ts": iso(nxt.ts)}
    r, out = first_start(49_800.0, -200.0, opens=[row], now=at("11:00", D2).ts)
    assert out == () and (r.session_start, r.session_floor) == (s.session_start, s.session_floor)
    assert r.acct.day_start_balance == s.acct.day_start_balance
    dry = make_setup(make_rules(), start=START)
    assert "day_start" not in kinds(core.on_bar(running(dry), nxt)[1])


def test_a_roll_that_breaches_journals_no_open():
    s = reconciled(core.start(sim(), T.ts)[0])
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(seconds=5), qty=1))  # held over the close
    s, effects = core.on_bar(core.on_account(s, account(), T.ts)[0], at("09:30", D2))
    assert s.halt_kind == "breach" and "day_start" not in kinds(effects)


@pytest.mark.parametrize("balance", [0.0, -5.0])
def test_a_balance_at_or_below_zero_is_not_a_figure(balance):
    """NinjaTrader before its account data loads (e.g. after it restarts) reports 0: the last
    figures stand, so no false safety-margin halt (review of #217, NB2)."""
    s = running(sim(), account(50_400.0))
    s2, effects = core.on_account(s, account(balance), T.ts)
    assert effects == () and s2.nt == s.nt == core.NtAccount(50_400.0, 0.0, 0.0)
    s2, effects = core.on_bar(s2, at("09:31"))
    assert s2.halted is None and "halt" not in kinds(effects)
    halted, _ = core.on_account(s, account(balance, is_sim=False), T.ts)
    assert halted.halted.startswith("mode mismatch: ")  # the mode is still checked


# #219: at an end_of_day roll, a floor from NinjaTrader's balance that alone breaches the app's
# own figures (P&L the app never saw) halts with "confirm balances", never a breach


def test_a_gap_against_an_unverified_open_loss_at_the_roll_halts_not_breaches():
    """The link dropped holding a long (its exit fill may be missed) with $200 open loss at the
    close: NinjaTrader's $51,900 puts the floor at -100, above the app's equity -200 though not
    its balance 0. The equity is what the rules check, so this is the gap's halt, not a breach."""
    s = reconciled(core.start(sim(), T.ts)[0])
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(seconds=5), qty=1))
    s, _ = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19990.0, 19990.0))
    s, _ = core.on_disconnect(s, "stream closed")
    s, _ = core.on_account(s, account(balance=51_900.0), T.ts)
    assert (s.acct.balance, s.acct.open_pnl, s.unreconciled) == (0.0, -200.0, core.LINK_LOST)
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith(
        "confirm balances: NinjaTrader's balance 51900.00 at the end-of-day session close puts"
        " the drawdown floor at 49900.00, at or above the app's equity 49800.00"
    )
    assert (s.halt_kind, s.acct.floor) == (None, -2000.0)
    assert peaks(effects) == [{"peak": 50_000.0, "ts": iso(at("09:30", D2).ts)}]


# NinjaTrader's equity 50,100 (through its floor), 50,200 (at it: a breach for the rules), 52,200
@pytest.mark.parametrize(("unrealized", "breach"), [(-2100.0, True), (-2000.0, True), (0.0, False)])
def test_an_open_loss_ninjatrader_still_holds_through_its_floor_breaches_at_the_roll(
    unrealized, breach
):
    """Review of #221, P1: the app and NinjaTrader agree at 52,200 (no gap), and a long with a
    known stop is held unverified over the close at -2,100 open. NinjaTrader's floor is 50,200.
    If NinjaTrader still holds that loss (its equity 50,100), it is a real breach, flattened as
    on main; if it shows none (the exit filled and the app never saw it), it is the gap's halt."""
    s = reconciled(core.start(sim(), T.ts)[0])
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(seconds=5), qty=1))
    s, _ = core.on_fill(s, {**fill_at(T.ts + timedelta(seconds=10), qty=-1), "price": 20110.0})
    s, _ = core.on_bar(s, at("09:31"))  # the exit's own bar
    assert (s.acct.balance, s.position.qty, s.halted) == (2200.0, 0, None)
    now = T.ts + timedelta(minutes=2)
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20000.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    assert (s.position.qty, s.position.stop) == (1, 19000.0)
    loss = 19895.0  # -105 points: -$2,100 open
    s, _ = core.on_bar(s, bar("09:32", 20000.0, 20000.0, loss, loss))
    assert (s.acct.open_pnl, s.halted) == (-2100.0, None)
    s, _ = core.on_disconnect(s, "stream closed")
    s, _ = core.on_account(s, account(52_200.0, today=2200.0, unrealized=unrealized), now)
    s, effects = core.on_bar(s, bar("09:30", loss, loss, loss, loss, D2))
    if breach:
        assert s.halt_kind == "breach"
        assert s.halted == "breach max_drawdown: equity 100.00 at or below drawdown floor 200.00"
        assert [type(e.action) for e in effects if isinstance(e, core.Send)] == [Flatten]
    else:
        assert s.halted.startswith("confirm balances: NinjaTrader's balance 52200.00 ")
        assert s.halt_kind is None and not [e for e in effects if isinstance(e, core.Send)]
