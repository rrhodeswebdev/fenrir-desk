"""22.2: NinjaTrader's account figures (DEC-4b), the sim/live mode check and kept peaks (DEC-4a)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, D3, bar, make_rules, rules_data
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
    # the app at 52,500: NinjaTrader's floor stays below it, so the raise is taken (#222)
    up = replace(START, balance=52_500.0)
    setup = make_setup(
        AccountRules.model_validate(d), mode="sim", start=up, day_start=DayStart(52_500.0, 50_000.0)
    )
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
        " 48000.00 from the armed balances and the journalled peaks; re-arm with the confirmed"
        " balances"
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
    assert kinds(effects) == ["alert"]  # #220: no longer silent
    assert s2.nt == s.nt == core.NtAccount(50_400.0, 0.0, 0.0)
    s2, effects = core.on_bar(s2, at("09:31"))
    assert s2.halted is None and "halt" not in kinds(effects)
    halted, _ = core.on_account(s, account(balance, is_sim=False), T.ts)
    assert halted.halted.startswith("mode mismatch: ")  # the mode is still checked


def test_a_zero_balance_while_running_shows_a_condition_and_alerts_once_per_run():
    """#220: an ignored balance at or below 0 is no longer silent. While it lasts the account has
    a "figures" condition, a pause like the stale and drift ones (owner decision: no new entries),
    and the run alerts once, however often it comes back."""
    s = running(sim(), account(50_400.0))
    figures = {"kind": "figures", "reason": core.NO_BALANCE}
    s, effects = core.on_account(s, account(0.0), T.ts)
    assert [(e.kind, e.payload) for e in effects] == [("alert", {"message": core.NO_BALANCE})]
    status = core.account_status(s)
    assert status["conditions"] == [figures]
    assert (status["halted"], status["halt_kind"], status["halt_reason"]) == (
        True,
        "figures",
        core.NO_BALANCE,
    )
    assert status["nt_balance"] == 50_400.0
    s, effects = core.on_account(s, account(-5.0), T.ts)
    assert effects == () and core.conditions(s) == [figures]
    s, _ = core.on_account(s, account(50_300.0), T.ts)
    assert core.conditions(s) == [] and s.nt.balance == 50_300.0
    s, effects = core.on_account(s, account(0.0), T.ts)
    assert effects == () and core.conditions(s) == [figures]  # one alert per run
    s, _ = core.halt(s, "strategy error")
    assert core.conditions(s) == [{"kind": None, "reason": "strategy error"}, figures]
    again, effects = core.on_account(running(sim(), account(50_400.0)), account(0.0), T.ts)
    assert kinds(effects) == ["alert"]  # a new run alerts again


def test_a_zero_balance_alerts_once_per_run_across_the_day_roll():
    """#220 review (C): the day roll keeps the run's alert flag, so a zero on a later day of the
    same run shows the condition again without a second alert."""
    s = running(sim(trailing="intraday"), account(50_400.0))
    s, effects = core.on_account(s, account(0.0), T.ts)
    assert kinds(effects) == ["alert"]
    s, _ = core.on_account(s, account(50_400.0), T.ts)
    s, _ = core.on_bar(s, at("09:30", D2))
    assert s.acct.day == D2 and s.halted is None
    s, effects = core.on_account(s, account(0.0), at("09:31", D2).ts)
    assert effects == () and core.conditions(s) == [{"kind": "figures", "reason": core.NO_BALANCE}]


def test_a_lost_link_pauses_entries_until_a_balance_above_0():
    """#243 (#220 review B): a lost link drops NinjaTrader's figures as stale, a zero's with
    them, and holds new entries back (a `figures` pause) until an account message with a balance
    above 0, not only until the reconnect's reconcile. The reconcile, a Resolve and the end of
    warm-up leave it; a zero after the reconnect shows as one, with no second alert in the run."""
    lost = {"kind": "figures", "reason": core.FIGURES_LOST}
    s = running(sim(), account(50_400.0))
    s, _ = core.on_account(s, account(0.0), T.ts)
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")
    assert [e.payload["message"] for e in effects] == [f"{core.LINK_LOST} (no heartbeat for 6 s)"]
    status = core.account_status(s)
    assert status["conditions"] == [lost] and status["nt_balance"] is None
    assert (status["halted"], status["halt_kind"], status["halt_reason"]) == (
        True,
        "figures",
        core.FIGURES_LOST,
    )
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")  # still the same outage
    assert effects == () and core.conditions(s) == [lost]
    s = reconciled(s)
    assert s.unreconciled is None and core.conditions(s) == [lost]
    s, _ = core.resolve(s, INSTRUMENT, (), (), T.ts)
    s, _ = core.warmed(replace(s, warming="warming up: replaying"), s.bars, (), ())
    assert core.conditions(s) == [lost]
    s, effects = core.on_account(s, account(0.0), T.ts)
    assert effects == () and core.conditions(s) == [{"kind": "figures", "reason": core.NO_BALANCE}]
    s, _ = core.on_account(s, account(50_300.0), T.ts)
    assert core.conditions(s) == [] and s.nt.balance == 50_300.0
    s, _ = core.on_disconnect(s, "stream closed")  # every lost link, a zero before it or not
    assert core.conditions(s) == [lost]


def test_a_lost_link_adds_no_figures_pause_to_an_account_that_never_started():
    """Without start figures `on_account` never takes a balance, so nothing could lift it."""
    s, _ = core.start(make_setup(make_rules(), mode="sim"), T.ts)
    s, _ = core.on_disconnect(s, "stream closed")
    assert "figures" not in [c["kind"] for c in core.conditions(s)]


def test_an_end_of_day_roll_during_a_zero_balance_takes_no_close():
    """#220 review: a balance at or below 0 at the session close is no close. The last figures
    above 0 may be hours old, so the roll journals no "peak" row and halts with "confirm
    balances", as with no figures at all. Its floor is never looser than main's, which raised it
    from those last figures (51,000 here: above the app's own balance, so they set it)."""
    s = running(sim(), account(51_000.0))
    s, _ = core.on_account(s, account(0.0), T.ts)
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted == core.NO_CLOSE and peaks(effects) == []
    assert (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)
    assert core.conditions(s)[-1] == {"kind": "figures", "reason": core.NO_BALANCE}  # still 0


def no_close_over_a_held_loss(lost, nt_balance=52_200.0):
    """A long held unverified over the close at -1,500 open, the app at 52,200 and NinjaTrader's
    last figure `nt_balance` (-1,500 open); then a zero (or the link drops again) before the
    reconcile, and the roll. Returns the state, the roll's effects and the bar's close."""
    s, close = held_over_the_close(2200.0, 1500.0, nt_balance, -1500.0)
    if lost == "zero":
        s, _ = core.on_account(s, account(0.0), T.ts)
    else:
        s, _ = core.on_disconnect(s, "stream closed")
    s, effects = core.on_bar(s, bar("09:30", close, close, close, close, D2))
    return s, effects, close


@pytest.mark.parametrize(
    ("lost", "nt_balance"), [("zero", 52_200.0), ("zero", 51_000.0), ("lost link", 52_200.0)]
)
def test_an_open_loss_held_over_a_close_taken_from_no_figures_still_breaches(lost, nt_balance):
    """#220 review (rounds 3 and 4, P5 and P4): the roll takes no close, but raises the floor
    from the app's own closing balance (200, as the gap branch does) with no "peak" row, so the
    loss running to equity 100 breaches and is flattened. With NinjaTrader's last figure 51,000,
    below the app's balance, the raise from it gives only -1,000: the app's own balance sets 200.
    Before, the lost link's floor stayed at the app's -1,300."""
    s, effects, close = no_close_over_a_held_loss(lost, nt_balance)
    assert s.halted == core.NO_CLOSE and peaks(effects) == [] and sends(effects) == []
    assert (s.acct.peak, s.acct.floor) == (2200.0, 200.0)
    low = close - 30.0  # equity 100
    s, effects = core.on_bar(s, bar("09:31", close, close, low, low, D2))
    assert s.halted == "breach max_drawdown: equity 100.00 at or below drawdown floor 200.00"
    assert sends(effects) == [Flatten]


def a_zero_over_the_close_flat(nt_balance):
    """The app flat at 0, NinjaTrader's last figure `nt_balance`, then a zero, then the roll."""
    s = running(sim(), account(nt_balance))
    s, _ = core.on_account(s, account(0.0), T.ts)
    s, effects = core.on_bar(s, at("09:30", D2))
    return s, effects, 20000.0


@pytest.mark.parametrize(
    ("roll", "floor"),
    [
        (lambda: no_close_over_a_held_loss("zero"), 200.0),
        (lambda: no_close_over_a_held_loss("lost link"), 200.0),
        (lambda: no_close_over_a_held_loss("zero", 53_000.0), 200.0),  # a stale gap: gap halt
        (lambda: a_zero_over_the_close_flat(51_000.0), -1000.0),  # raised from the last figure
    ],
    ids=["zero", "lost link", "zero, stale gap", "zero, stale figure above the app"],
)
def test_a_restart_after_a_no_close_roll_keeps_its_floor(roll, floor):
    """#220 review (round 4, P12/P13): a roll that takes no close journals no "peak" row, so a
    restart later that day rebuilt its peak from the pins and the earlier rows (floor -2,000;
    main, from a stale "peak" row, 200). Today's journalled open, which the roll wrote with the
    peak it raised to, is a lower bound on an end_of_day restart's peak: the restart keeps the
    running floor and halt, still with no "peak" row, so a later restart asks again. That holds
    under a gap halt from stale figures too (no "confirm balances" row then), and for a raise
    from NinjaTrader's last figure above the app's balance (the open's peak, not its balance)."""
    s, effects, close = roll()
    assert s.halted.startswith("confirm balances: ") and s.acct.floor == floor
    rows = [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    halts, opens = ([p for k, p in rows if k == kind] for kind in ("halt", "day_start"))
    assert peaks(effects) == [] and len(opens) == 1
    balance = START.phase_start + s.acct.balance
    setup = replace(sim(), start=Start(50_000.0, 50_000.0, balance), day_start=None)
    now = bar("10:00", close, close, close, close, D2).ts
    r, out = core.start(setup, now, halts, [], opens)
    assert (r.halted, r.acct.peak, r.acct.floor) == (s.halted, s.acct.peak, floor)
    assert r.session_floor == floor and peaks(out) == []


NO_CLOSE_ROLLS = {
    "zero": lambda: no_close_over_a_held_loss("zero"),
    "lost link": lambda: no_close_over_a_held_loss("lost link"),
    "zero, stale gap": lambda: no_close_over_a_held_loss("zero", 53_000.0),
    "zero, stale 50,700": lambda: no_close_over_a_held_loss("zero", 50_700.0),
}


def restart_after(roll, now, nt_balance):
    """`start` from the roll's own journalled rows, armed before D1, NinjaTrader at
    `nt_balance`."""
    s, effects, close = roll()
    rows = [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    halts, opens = ([p for k, p in rows if k == kind] for kind in ("halt", "day_start"))
    setup = replace(sim(), start=Start(50_000.0, 50_000.0, nt_balance), day_start=None)
    setup = armed_at(setup, at("08:00").ts)
    r, out = core.start(setup, now, halts, [], opens)
    return s, r, out, close


@pytest.mark.parametrize("nt_balance", [50_150.0, 50_200.0])
@pytest.mark.parametrize("roll", NO_CLOSE_ROLLS.values(), ids=NO_CLOSE_ROLLS.keys())
def test_a_restart_at_or_below_a_no_close_floor_cannot_start(roll, nt_balance):
    """#220 review (round 5, blocking): the start's liquidation check counts today's open's peak,
    so a same-day restart with NinjaTrader at or below the running floor (200) cannot start,
    never breaching (or flattening) on NinjaTrader's figure alone."""
    s, r, _, close = restart_after(roll, bar("10:00", 0, 0, 0, 0, D2).ts, nt_balance)
    assert s.acct.floor == 200.0
    assert not r.started and r.halt_kind is None
    r, effects = core.on_bar(r, bar("10:01", close, close, close, close, D2))
    assert r.halt_kind != "breach" and sends(effects) == []


@pytest.mark.parametrize(
    ("roll", "peak"),
    [
        (NO_CLOSE_ROLLS["zero"], 2200.0),
        (NO_CLOSE_ROLLS["lost link"], 2200.0),
        (NO_CLOSE_ROLLS["zero, stale gap"], 3000.0),  # NinjaTrader's raised peak, as on main
        (NO_CLOSE_ROLLS["zero, stale 50,700"], 2200.0),
    ],
    ids=NO_CLOSE_ROLLS.keys(),
)
def test_a_later_day_restart_keeps_a_no_close_floor(roll, peak):
    """#220 review (round 5, NB1): down over the next roll too, so no open journalled for today:
    the highest open journalled since arming bounds the peak, so a D3 restart keeps the floor the
    D2 no-close roll raised (main, from that roll's stale "peak" row, 200), with no "peak" row.
    Under a gap from the stale figures, the open carries NinjaTrader's raised peak as its
    "nt_peak", which a later-day restart takes as it takes a "peak" row's (#225), as on main."""
    s, r, out, _ = restart_after(roll, bar("10:00", 0, 0, 0, 0, D3).ts, 52_200.0)
    assert (s.acct.peak, s.acct.floor) == (2200.0, 200.0)
    assert (r.acct.peak, r.acct.floor) == (peak, peak - 2000.0)
    assert r.halted.startswith("confirm balances: ") and peaks(out) == []


def test_a_restart_takes_ninjatraders_peak_for_its_own_floor_from_an_open_too():
    """#220 x #239: a roll during a zero under a stale gap journals no "peak" row; its open carries
    NinjaTrader's raised peak (53,000). A same-day restart takes it back for NinjaTrader's own
    floor (51,000), so NinjaTrader at 51,000 is at it: the halt."""
    s, r, _, _ = restart_after(NO_CLOSE_ROLLS["zero, stale gap"], at("10:00", D2).ts, 52_200.0)
    assert r.started and r.halted == s.halted and r.nt_peak == 3000.0
    r, effects = core.on_account(reconciled(r), account(51_000.0), at("10:01", D2).ts)
    caught = nt_floor(51_000.0, floor=51_000.0, peak=53_000.0)
    assert alerts(effects) == [f"{caught} (still halted: {s.halted})"]


def test_the_no_close_floor_trails_the_apps_balance_under_a_gap_halt_too():
    """#220 review (round 4, M12): a gap halt at the D2 roll with a long of 2 held, whose Flatten
    fills only 1 (at +600) before the link drops again over the next close. The D3 roll's
    "confirm balances" is a no-op under the gap halt, but its floor still trails the app's own
    balance, 600 (floor -1,400), above the close's equity, 400, which the rules' own trail takes."""
    s = reconciled(core.start(sim(), T.ts)[0])
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(seconds=5), qty=2))
    s, _ = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19990.0, 19990.0))
    s, _ = core.on_disconnect(s, "stream closed")
    s, _ = core.on_account(s, account(balance=52_000.0, unrealized=-400.0), T.ts)
    s, effects = core.on_bar(s, bar("09:30", 19990.0, 19990.0, 19990.0, 19990.0, D2))
    assert s.halted.startswith(core.GAP) and sends(effects) == [Flatten]
    gap = s.halted
    part = {**fill_at(at("09:30", D2).ts + timedelta(seconds=5), qty=-1), "price": 20030.0}
    s, _ = core.on_fill(s, part)
    assert (s.acct.balance, s.position.qty) == (600.0, 1)
    s, _ = core.on_disconnect(s, "stream closed")
    s, effects = core.on_bar(s, bar("09:30", 19990.0, 19990.0, 19990.0, 19990.0, D3))
    assert s.halted == gap and peaks(effects) == []
    assert (s.acct.peak, s.acct.floor) == (600.0, -1400.0)


def test_a_zero_at_the_roll_replaces_a_day_halt_with_confirm_balances():
    """#220 review (round 3, mutant D): under a safety day halt (lifted by this very roll), a
    zero at the close is still no close: the new day must not trade on it."""
    s = running(sim(daily_loss=1000.0), account(today=-850.0))
    assert s.halt_kind == "safety" and s.halt_day is not None
    s, _ = core.on_account(s, account(0.0), T.ts)
    s, effects = core.on_bar(s, at("09:30", D2))
    assert (s.halted, s.halt_kind, s.halt_day) == (core.NO_CLOSE, None, None)
    assert peaks(effects) == []


def test_a_zero_balance_is_listed_with_the_pauses_before_warming_up():
    """#220 review (M8), owner decision: `figures` is a pause, listed after the drift pause and
    before a `warming` condition (#79), as `halt_reason` takes them; under a stale pause the
    stale one is the account's `halt_kind`."""
    s = running(sim(), account(50_400.0))
    s, _ = core.on_account(replace(s, warming="warming up: replaying"), account(0.0), T.ts)
    assert [c["kind"] for c in core.conditions(s)] == ["figures", "warming"]
    status = core.account_status(s)  # #243 (R6): `halt_reason` takes them in the same order
    assert (status["halt_kind"], status["halt_reason"]) == ("figures", core.NO_BALANCE)
    status = core.account_status(replace(s, stale="no NQ bar"))
    assert [c["kind"] for c in status["conditions"]] == ["stale", "figures", "warming"]
    assert (status["halt_kind"], status["halt_reason"]) == ("stale", "no NQ bar")


def test_a_zero_balance_under_a_gap_halt_keeps_the_halt_and_its_flatten():
    """#220 with #229: the app flat, NinjaTrader with open P&L, the gap halt's account-wide
    Flatten out. A zero balance is no figure, so it never ends that Flatten (its 0 open P&L is
    not NinjaTrader holding nothing) and nothing more is sent. The next roll during it takes no
    close, and its "confirm balances" never replaces the open-ended gap halt."""
    s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=-1900.0))
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith(core.GAP) and sends(effects) == [Flatten]
    gap = s.halted
    s, effects = core.on_account(s, account(0.0), T.ts)
    assert kinds(effects) == ["alert"] and s.exiting is not None
    assert core.conditions(s)[-1] == {"kind": "figures", "reason": core.NO_BALANCE}
    assert sends(core.on_bar(s, at("09:31", D2))[1]) == []
    s, effects = core.on_bar(s, at("09:30", D3))
    assert s.halted == gap and peaks(effects) == [] and "halt" not in kinds(effects)


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
        "confirm balances: a gap at the end-of-day session close: NinjaTrader's balance 51900.00"
        " puts the drawdown floor at 49900.00, at or above the app's equity 49800.00"
    )
    assert (s.halt_kind, s.acct.floor) == (None, -2000.0)
    stamp = iso(at("09:30", D2).ts)
    assert peaks(effects) == [{"peak": 50_000.0, "ts": stamp, "nt_peak": 51_900.0}]


# #225: a gap halt journals NinjaTrader's raised peak next to the app's own. A later-day restart
# opens at NinjaTrader's balance, which holds the gap, so it takes that peak too (the stricter
# floor, NinjaTrader's); a same-day restart rebuilds the app's own balance and keeps its peak


def gap_rolled():
    """#219's gap at the D1 close: the app at 50,000, NinjaTrader at 52,200 (its floor 50,200).
    Returns the state after the D2 roll and the "halt" and "peak" payloads journalled."""
    s, effects = core.on_bar(
        running(sim(), account(balance=52_200.0, today=2200.0)), at("09:30", D2)
    )
    rows = [e.payload for e in effects if isinstance(e, Log) and e.kind == "halt"]
    return s, rows, peaks(effects)


def restart(balance, halts, kept, opens=(), day=D3, trailing="end_of_day", realized=0.0):
    """A sim restart at 09:30 ET on `day` from NinjaTrader's first message (`balance`), with no
    day_start given and what was journalled since arming."""
    start = replace(START, balance=balance, realized_today=realized)
    setup = replace(sim(trailing=trailing), start=start, day_start=None)
    return core.start(setup, at("09:30", day).ts, halts, kept, opens)


def test_the_gap_roll_journals_ninjatraders_raised_peak_next_to_the_apps():
    s, _, kept = gap_rolled()
    assert s.halted.startswith(core.GAP) and (s.acct.peak, s.acct.floor) == (0.0, -2000.0)
    stamp = iso(at("09:30", D2).ts)
    assert kept == [{"peak": 50_000.0, "ts": stamp, "nt_peak": 52_200.0}]


def test_a_later_day_restart_under_the_gap_halt_takes_ninjatraders_peak():
    """The restart opens at NinjaTrader's 52,200 with no open journalled for D3: its peak is
    NinjaTrader's 52,200 (floor 50,200), not the app's 50,000 (floor 48,000, looser by the gap).
    The open journalled holds that peak, and a loss through NinjaTrader's floor breaches."""
    _, halts, kept = gap_rolled()
    s, effects = restart(52_200.0, halts, kept)
    assert s.halted.startswith(core.GAP) and s.halt_kind is None
    assert (s.acct.balance, s.acct.peak, s.acct.floor, s.session_floor) == (
        2200.0,
        2200.0,
        200.0,
        200.0,
    )
    stamp = iso(at("09:30", D3).ts)
    assert [e.payload for e in effects if e.kind == "day_start"] == [
        {**opened(D3, 52_200.0, peak=52_200.0), "ts": stamp}
    ]
    s = reconciled(s)
    assert core.account_status(s)["limits"]["drawdown_remaining"] == 2000.0
    s, _ = core.on_fill(s, fill_at(at("09:30", D3).ts + timedelta(seconds=5), qty=1))
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19900.0, 19900.0, D3))  # -2,000
    breach = "breach max_drawdown: equity 200.00 at or below drawdown floor 200.00"
    assert s.halted == breach
    # the figures are NinjaTrader's since the restart: the clause holds for them as for the app's
    gap = halts[0]["reason"]
    assert alerts(effects) == [f"{breach}{NOT_THE_GAP} (was halted: {gap})"]


def test_a_same_day_restart_under_the_gap_halt_keeps_the_apps_own_peak():
    """D2 has the roll's open journalled (the app's 50,000 and peak): the replay puts the app's
    own balance back, so NinjaTrader's peak would breach it (#221's M5). The app's floor holds."""
    _, halts, kept = gap_rolled()
    roll = opened(D2, 50_000.0)
    s, _ = restart(52_200.0, halts, kept, opens=[roll], day=D2, realized=2200.0)
    s = reconciled(core.replay(s, []))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (0.0, 0.0, -2000.0)
    s, _ = core.on_bar(s, at("09:31", D2))
    assert s.halted.startswith(core.GAP) and s.halt_kind is None


def test_a_same_day_restart_after_a_later_day_restart_keeps_ninjatraders_peak():
    """The later-day restart journalled D3's open at NinjaTrader's balance and peak, and no
    "peak" row (end_of_day: only a roll writes one): a second restart that day opens at those
    figures again, never at the app's peak against NinjaTrader's balance."""
    _, halts, kept = gap_rolled()
    _, effects = restart(52_200.0, halts, kept)
    opens = [e.payload for e in effects if e.kind == "day_start"]
    s, _ = restart(52_200.0, halts, kept, opens=opens)
    s = reconciled(core.replay(s, []))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2200.0, 2200.0, 200.0)


def test_a_same_day_intraday_restart_after_a_later_day_restart_keeps_ninjatraders_peak():
    """The intraday side (review of #238, round 2): NinjaTrader peaked at 52,300 under the D1
    halt and stands at 52,100 on D2. The later-day restart opens D2 at 52,100 with peak 52,300
    and journals no "peak" row (its peak already holds the nt_peak); a second restart that day
    takes that open's peak, not the app's 50,000 raised only to the replayed balance."""
    s = running(sim(trailing="intraday"))
    s, one = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    s, two = core.on_account(s, account(balance=52_300.0, today=2300.0), T.ts)
    halts = [e.payload for e in one if e.kind == "halt"]
    kept = peaks(one) + peaks(two)
    first, effects = restart(52_100.0, halts, kept, day=D2, trailing="intraday")
    assert peaks(effects) == []
    opens = [e.payload for e in effects if e.kind == "day_start"]
    assert [(o["balance"], o["peak"]) for o in opens] == [(52_100.0, 52_300.0)]
    s, _ = restart(52_100.0, halts, kept, opens=opens, day=D2, trailing="intraday")
    s = reconciled(core.replay(s, []))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2100.0, 2300.0, 300.0)
    assert (first.acct.peak, first.acct.floor) == (s.acct.peak, s.acct.floor)


def test_a_later_day_restart_at_ninjatraders_floor_cannot_start():
    """NinjaTrader at 50,200, its own floor, at the restart: that is no start (the liquidation
    level from NinjaTrader's peak), where the app's own peak would have started it 2,200 above
    a looser floor. The restored gap halt stays the reason shown."""
    _, halts, kept = gap_rolled()
    s, effects = restart(50_200.0, halts, kept)
    assert not s.started and s.halted == halts[0]["reason"]
    # the restored halt is not replaced, so an alert says why it did not start (review of #238)
    assert kinds(effects) == ["alert"]
    assert alerts(effects) == [
        "cannot start: confirm balances: NinjaTrader's balance 50200.00 is at or below the"
        " liquidation level 50200.00 from the armed balances and the journalled peaks; re-arm with"
        f" the confirmed balances (still halted: {halts[0]['reason']})"
    ]
    assert restart(50_200.01, halts, kept)[0].started


def test_a_first_start_at_the_liquidation_level_journals_its_halt_not_an_alert():
    """With no halt in force the refusal is the halt itself, as before; a restart under that
    same halt says nothing more."""
    s, effects = restart(48_000.0, [], [], day=D1)
    assert not s.started and kinds(effects) == ["halt"]
    halts = [e.payload for e in effects]
    assert restart(48_000.0, halts, [], day=D1)[1] == ()


def test_every_gap_roll_journals_ninjatraders_raised_peak():
    """R15 (review of #238): NinjaTrader closes D2 at 53,000 under the halt, still a gap; that
    roll's row holds 53,000 too, and a later-day restart takes it (floor 51,000)."""
    s, halts, kept = gap_rolled()
    s = core.on_account(s, account(balance=53_000.0, today=800.0), at("09:30", D2).ts)[0]
    s, effects = core.on_bar(s, at("09:30", D3))
    assert s.halted == halts[0]["reason"] and s.acct.floor == -2000.0
    stamp = iso(at("09:30", D3).ts)
    assert peaks(effects) == [{"peak": 50_000.0, "ts": stamp, "nt_peak": 53_000.0}]
    s, _ = restart(53_000.0, halts, kept + peaks(effects), day=D3 + timedelta(days=1))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (3000.0, 3000.0, 1000.0)


def test_a_later_day_restart_takes_the_highest_nt_peak_not_the_latest_row():
    """M13: the D2 close takes NinjaTrader's raise at 51,000 (no gap: the floor -1,000 is below
    the app's 0), a peak lower than the D1 gap's 52,200. NinjaTrader's peak is still 52,200, so
    a restart on D4 takes it, not the latest row's 51,000."""
    s, halts, kept = gap_rolled()
    s = core.on_account(s, account(balance=51_000.0, today=-1200.0), at("09:30", D2).ts)[0]
    s, effects = core.on_bar(s, at("09:30", D3))
    assert peaks(effects) == [{"peak": 51_000.0, "ts": iso(at("09:30", D3).ts)}]
    s, _ = restart(51_000.0, halts, kept + peaks(effects), day=D3 + timedelta(days=1))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (1000.0, 2200.0, 200.0)


def test_a_restart_with_peak_rows_journalled_before_225_still_starts():
    """A "peak" row from before #225 has no "nt_peak": it counts as the app's peak, as before."""
    kept = [{"peak": 50_700.0, "ts": iso(T.ts)}]
    s, _ = restart(50_700.0, [], kept, day=D1)
    assert (s.acct.peak, s.acct.floor) == (700.0, -1300.0)


def test_an_intraday_gap_halt_journals_ninjatraders_peak_each_time_it_rises():
    """Under the intraday gap halt NinjaTrader's raise is not taken: its peak goes in a "peak"
    row next to the app's at the halt, and again only when it rises above the last one."""
    s = running(sim(trailing="intraday"))
    ts = [T.ts + timedelta(seconds=2 * n) for n in range(4)]
    s, effects = core.on_account(s, account(balance=52_200.0, today=2200.0), ts[0])
    assert kinds(effects) == ["halt", "peak"]
    assert peaks(effects) == [{"peak": 50_000.0, "ts": iso(ts[0]), "nt_peak": 52_200.0}]
    for n, balance in ((1, 52_200.0), (2, 52_100.0)):  # not above it: no row
        s, effects = core.on_account(s, account(balance=balance, today=2200.0), ts[n])
        assert effects == () and s.acct.floor == -2000.0
    s, effects = core.on_account(s, account(balance=52_300.0, today=2300.0), ts[3])
    assert peaks(effects) == [{"peak": 50_000.0, "ts": iso(ts[3]), "nt_peak": 52_300.0}]


def test_an_intraday_gap_message_still_journals_the_apps_own_raise_when_theirs_did_not_rise():
    """M14: after the halt's row (NinjaTrader at 54,000), the app books +1,000 with no bar since;
    the next message, NinjaTrader's peak where it was, raises the app's peak to its own balance
    and journals that alone."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=54_000.0, today=4000.0), T.ts)
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    exit_ = {**fill_at(T.ts + timedelta(minutes=1, seconds=10), qty=-1), "price": 20050.0}
    s, _ = core.on_fill(s, exit_)
    ts = T.ts + timedelta(minutes=1, seconds=12)
    s, effects = core.on_account(s, account(balance=54_000.0, today=4000.0), ts)
    assert s.halted.startswith(GAP_INTRADAY) and (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)
    assert peaks(effects) == [{"peak": 51_000.0, "ts": iso(ts)}]


def test_a_later_day_intraday_restart_under_the_gap_halt_takes_ninjatraders_highest_peak():
    """NinjaTrader peaked at 52,300 under the halt and stands at 52,100 at the restart (its
    flatten closed lower): the restart's peak is 52,300, NinjaTrader's (floor 50,300), not its
    balance now. A same-day restart keeps the app's own, 50,000."""
    s = running(sim(trailing="intraday"))
    s, one = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    s, two = core.on_account(s, account(balance=52_300.0, today=2300.0), T.ts)
    halts = [e.payload for e in one if e.kind == "halt"]
    kept = peaks(one) + peaks(two)
    s, _ = restart(52_100.0, halts, kept, day=D2, trailing="intraday")
    assert s.halted.startswith(GAP_INTRADAY)
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2100.0, 2300.0, 300.0)
    same, _ = restart(
        52_100.0, halts, kept, opens=[opened(D1, 50_000.0)], day=D1, trailing="intraday"
    )
    same = core.replay(same, [])
    assert (same.acct.balance, same.acct.peak, same.acct.floor) == (0.0, 0.0, -2000.0)


# #239: after a gap halt NinjaTrader's peak can stay above the app's: the D2 close takes
# NinjaTrader's lower raise (M13, review of #238). NinjaTrader's own equity is then checked
# against its own floor (from that peak) on every account message, so the runner's drawdown check
# is never looser than NinjaTrader's, and never mixes the app's figures with NinjaTrader's floor


def nt_floor(equity, floor=50_200.0, peak=52_200.0):
    """The reason of the halt at NinjaTrader's own floor, absolute $."""
    return (
        f"confirm balances: a gap halt journalled NinjaTrader's peak {peak:.2f}: NinjaTrader's"
        f" equity {equity:.2f} is at or below its own drawdown floor {floor:.2f} from that peak"
        " (its account is through the drawdown limit there); any position the app holds is"
        " flattened, and the whole account while NinjaTrader shows open P&L; re-arm with the"
        " confirmed balances"
    )


def shrunk():
    """M13: #219's gap at the D1 close (the app at 50,000, NinjaTrader at 52,200, its floor
    50,200), then NinjaTrader closes D2 at 51,000 (-1,200 the app never saw): no gap, so that roll
    takes NinjaTrader's raise at 51,000 (the app's floor 49,000, looser than NinjaTrader's by
    1,200). Returns the state on D3 and the "halt", "peak" and "day_start" payloads journalled."""
    s, halts, kept = gap_rolled()
    s = core.on_account(s, account(balance=51_000.0, today=-1200.0), at("09:30", D2).ts)[0]
    s, effects = core.on_bar(s, at("09:30", D3))
    assert s.halted == halts[0]["reason"] and (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)
    opens = [e.payload for e in effects if e.kind == "day_start"]
    s = core.on_account(s, account(balance=51_000.0), at("09:30", D3).ts)[0]  # D3's figures
    return reconciled(s), halts, kept + peaks(effects), opens


def manual_loss(s, loss, day=D3):
    """A manual 1 NQ on the armed contract, closed `loss` lower within the minute (the runner's
    flatten of a position with no known stop is out meanwhile); returns the state and the rows."""
    ts = at("09:31", day).ts
    s, one = core.on_fill(s, fill_at(ts + timedelta(seconds=5), qty=1))
    exit_ = {**fill_at(ts + timedelta(seconds=10), qty=-1), "price": 20000.0 - loss / 20}
    s, two = core.on_fill(s, exit_)
    assert (s.position.qty, s.acct.balance) == (0, -loss)
    return s, [("fill", e.payload) for e in (*one, *two) if isinstance(e, Log) and e.kind == "fill"]


def test_a_loss_through_ninjatraders_own_floor_after_the_gap_shrank_halts_and_alerts():
    """M13 while running: a manual -900 puts NinjaTrader at 50,100, through its own floor 50,200,
    while the app's -900 is above the app's floor -1,000. That used to pass with nothing said and
    1,000 of room shown; the message now halts (the gap halt holds, so an alert says it) and the
    room shown is NinjaTrader's own, -100. Once per run: the next message says nothing more."""
    s, halts, _, _ = shrunk()
    assert core.account_status(s)["limits"]["drawdown_remaining"] == 800.0  # 1,000 at NinjaTrader
    s, _ = manual_loss(s, 900.0)
    now = at("09:32", D3).ts
    s, effects = core.on_account(s, account(balance=50_100.0, today=-900.0), now)
    reason = nt_floor(50_100.0)
    assert kinds(effects) == ["alert"]  # flat, and NinjaTrader holds nothing: nothing to send
    assert alerts(effects) == [f"{reason} (still halted: {halts[0]['reason']})"]
    assert (s.halted, s.halt_kind) == (halts[0]["reason"], None)  # no breach (22.2)
    assert (s.acct.balance, s.acct.floor) == (-900.0, -1000.0)  # the app's own figures stay
    assert core.account_status(s)["limits"]["drawdown_remaining"] == -100.0
    assert core.on_account(s, account(balance=50_000.0, today=-1000.0), now)[1] == ()


@pytest.mark.parametrize(
    ("balance", "unrealized", "through"),
    [
        (50_200.0, 0.0, True),  # at NinjaTrader's floor
        (50_200.004, 0.0, True),  # at it, to the cent
        (50_200.01, 0.0, False),
        (51_000.0, -800.0, True),  # its equity: balance plus open P&L
        (51_000.0, -799.99, False),
    ],
)
def test_ninjatraders_own_floor_is_checked_on_its_equity_to_the_cent(balance, unrealized, through):
    """At or below NinjaTrader's floor (to the cent, on balance plus open P&L) halts; a cent above
    does not. NinjaTrader's open P&L is a position the app does not know of: flattened at once,
    account-wide, on that message."""
    s, _, _, _ = shrunk()
    s, effects = core.on_account(s, account(balance=balance, unrealized=unrealized), T.ts)
    assert bool(alerts(effects)) is through
    assert sends(effects) == ([Flatten] if through and unrealized else [])
    if through and unrealized:
        assert actions(effects)[0]["reason"] == nt_floor(50_200.0)


def test_a_same_day_restart_after_the_gap_shrank_checks_ninjatraders_floor_too():
    """M13 after a same-day restart: it replays the app's own balance and keeps the app's peak
    (#221's M5), and takes NinjaTrader's journalled peak back as its floor's (`nt_peak`), so the
    first message after it halts as running did. Before the loss nothing is said."""
    s, halts, kept, opens = shrunk()
    _, rows = manual_loss(s, 900.0)
    s, effects = restart(50_100.0, halts, kept, opens=opens, realized=-900.0)
    assert s.started and effects == () and s.nt_peak == 2200.0
    s = reconciled(core.replay(s, rows))
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (-900.0, 1000.0, -1000.0)
    now = at("09:33", D3).ts
    s, effects = core.on_account(s, account(balance=50_100.0, today=-900.0), now)
    reason = nt_floor(50_100.0)
    assert alerts(effects) == [f"{reason} (still halted: {halts[0]['reason']})"]
    # the plain gap halt's same-day restart: NinjaTrader's equity is its peak, far above its floor
    _, halts, kept = gap_rolled()
    s, _ = restart(52_200.0, halts, kept, opens=[opened(D2, 50_000.0)], day=D2, realized=2200.0)
    s = reconciled(core.replay(s, []))
    assert core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)[1] == ()


def test_a_loss_through_ninjatraders_own_intraday_floor_after_the_gap_shrank_halts():
    """The intraday side: the gap halt at NinjaTrader's 52,200 (its floor 50,200), then its
    51,000 takes the raise (no gap: the floor -1,000 is below the app's 0); at 50,200 with -100
    open NinjaTrader is at its own floor: the halt, and its account-wide flatten on that message."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    gap = s.halted
    s, _ = core.on_account(s, account(balance=51_000.0, today=1000.0), T.ts)
    assert (s.halted, s.acct.peak, s.acct.floor, s.nt_peak) == (gap, 1000.0, -1000.0, 2200.0)
    s, effects = core.on_account(s, account(balance=50_300.0, unrealized=-100.0), T.ts)
    reason = nt_floor(50_200.0)
    assert alerts(effects) == [f"{reason} (still halted: {gap})"]
    assert sends(effects) == [Flatten] and actions(effects)[0]["reason"] == reason


def test_the_halt_at_ninjatraders_own_floor_holds_when_nothing_else_does():
    """The rule fails closed whatever halt is in force: with NinjaTrader's peak known above the
    app's and no halt (not a state the runner reaches today: that peak comes with the gap halt),
    it is a gap halt of its own, journalled, and flattens as one does, also on later bars."""
    s = replace(running(sim()), nt_peak=2200.0)
    s, effects = core.on_account(s, account(balance=50_300.0, unrealized=-100.0), T.ts)
    reason = nt_floor(50_200.0)
    assert (s.halted, s.halt_kind, s.halt_day) == (reason, None, None)
    assert kinds(effects) == ["halt", "action", "send"]
    s = core.send_failed(s, s.exiting, "refused")[0]
    assert sends(core.on_bar(s, at("09:31"))[1]) == [Flatten]  # `_exit`, as under any gap halt


def test_ninjatraders_own_floor_is_not_checked_on_a_refused_start():
    """A refused start's figures are not the account's (#240): its rules may have been edited."""
    s, halts, kept, opens = shrunk()
    start = replace(START, balance=50_100.0, realized_today=-900.0)
    setup = replace(sim(), start=start, day_start=None, strategy_hash="edited")
    s, _ = core.start(setup, at("09:30", D3).ts, halts, kept, opens)
    assert not s.started
    assert core.on_account(s, account(balance=50_100.0, today=-900.0), T.ts)[1] == ()


def test_the_message_that_lifts_a_lost_links_figures_pause_checks_ninjatraders_floor():
    """#243 x #239: a lost link drops NinjaTrader's figures and pauses on `figures`; the first
    message above 0 after it lifts that pause and is checked like any other."""
    s, halts, _, _ = shrunk()
    s, _ = core.on_disconnect(s, "stream closed")
    assert s.no_balance == core.FIGURES_LOST and s.nt is None
    s, effects = core.on_account(s, account(balance=50_100.0, today=-900.0), at("09:32", D3).ts)
    assert s.no_balance is None
    assert alerts(effects) == [f"{nt_floor(50_100.0)} (still halted: {halts[0]['reason']})"]


def test_a_later_day_restart_checks_ninjatraders_own_floor_on_its_own_equity_too():
    """After a later-day restart the app's peak is NinjaTrader's (#225), so the two floors agree.
    A loss NinjaTrader books that the app never sees (another instrument) through that floor
    leaves the app's balance where it was, so no rule event sees it (22.2): this halt does."""
    _, halts, kept = gap_rolled()
    s, _ = restart(52_200.0, halts, kept)
    s = reconciled(s)
    assert (s.acct.floor, s.nt_peak) == (200.0, 2200.0)
    s, effects = core.on_account(s, account(balance=50_200.0, today=-2000.0), at("09:31", D3).ts)
    assert alerts(effects) == [f"{nt_floor(50_200.0)} (still halted: {halts[0]['reason']})"]
    assert (s.acct.balance, s.halt_kind) == (2200.0, None)


def test_ninjatraders_own_floor_is_from_its_highest_peak_not_its_latest():
    """Two gap rolls: NinjaTrader closes D1 at 52,200 and D2 at 52,100 (a gap still). Its floor
    stays 50,200, running and after a same-day restart, so 50,150 is through it."""
    s, halts, kept = gap_rolled()
    s = core.on_account(s, account(balance=52_100.0, today=-100.0), at("09:30", D2).ts)[0]
    s, effects = core.on_bar(s, at("09:30", D3))
    assert peaks(effects)[0]["nt_peak"] == 52_100.0 and s.nt_peak == 2200.0
    kept += peaks(effects)
    opens = [e.payload for e in effects if e.kind == "day_start"]
    message = account(balance=50_150.0, today=-1950.0)
    caught = [f"{nt_floor(50_150.0)} (still halted: {halts[0]['reason']})"]
    s, effects = core.on_account(reconciled(s), message, at("09:31", D3).ts)
    assert alerts(effects) == caught
    s, _ = restart(50_150.0, halts, kept, opens=opens, realized=-1950.0)
    s = reconciled(core.replay(s, []))
    assert alerts(core.on_account(s, message, at("09:32", D3).ts)[1]) == caught


def test_the_halt_at_ninjatraders_own_floor_flattens_what_the_app_holds_once():
    """A long the app adopted with its stop (reconciled: no Flatten out) is flattened on the
    message, whatever open P&L NinjaTrader shows (a stale 0 here); with a Flatten already out,
    no second one."""
    s, _, _, _ = shrunk()
    now = at("09:31", D3).ts
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20000.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    assert (s.position.qty, s.position.stop, s.exiting) == (1, 19000.0, None)
    message = account(balance=50_100.0, today=-900.0)
    _, effects = core.on_account(s, message, now)
    assert sends(effects) == [Flatten] and actions(effects)[0]["reason"] == nt_floor(50_100.0)
    _, effects = core.on_account(replace(s, exiting="out"), message, now)
    assert alerts(effects) and sends(effects) == []


def test_ninjatraders_own_floor_is_checked_on_an_intraday_gap_message_too():
    """K2: the intraday gap halt at NinjaTrader's 52,200, then a manual -1,900 both book (the app
    at -1,900, its floor -2,000). NinjaTrader at 50,300 is a gap again (the floor -1,700 from it is
    above the app's equity), above its own floor 50,200; at 50,150, -150 the app never saw, still
    a gap and through that floor: the halt."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    gap = s.halted
    s, _ = manual_loss(reconciled(s), 1900.0, day=D1)
    now = at("09:32").ts
    s, effects = core.on_account(s, account(balance=50_300.0, today=300.0), now)
    assert alerts(effects) == [] and (s.acct.floor, s.nt_peak) == (-2000.0, 2200.0)
    s, effects = core.on_account(s, account(balance=50_150.0, today=150.0), now)
    assert alerts(effects) == [f"{nt_floor(50_150.0)} (still halted: {gap})"]


D4 = D3 + timedelta(days=1)


def test_ninjatraders_own_peak_rises_with_a_close_the_app_takes_too():
    """K23: after M13 a manual +1,300 both book puts NinjaTrader at 52,300; the D3 close is no
    gap and takes the raise (the app's peak 2,300). NinjaTrader's own peak is 52,300 now, not the
    gap's 52,200, so -2,050 it alone books (50,250) is through its own floor 50,300."""
    s, halts, _, _ = shrunk()
    s, _ = manual_loss(s, -1300.0)
    s = core.on_account(s, account(balance=52_300.0, today=1300.0), at("09:32", D3).ts)[0]
    s, _ = core.on_bar(s, at("09:30", D4))
    assert (s.acct.peak, s.acct.floor, s.nt_peak) == (2300.0, 300.0, 2300.0)
    s, effects = core.on_account(s, account(balance=50_250.0, today=-2050.0), at("09:31", D4).ts)
    caught = nt_floor(50_250.0, floor=50_300.0, peak=52_300.0)
    assert alerts(effects) == [f"{caught} (still halted: {halts[0]['reason']})"]


def test_ninjatraders_own_peak_never_rises_on_a_close_during_a_zero():
    """#220 x #239: the same close during a zero is no close (its figures may be hours old), so
    NinjaTrader's own peak stays the gap's 52,200."""
    s, _, _, _ = shrunk()
    s, _ = manual_loss(s, -1300.0)
    s = core.on_account(s, account(balance=52_300.0, today=1300.0), at("09:32", D3).ts)[0]
    s = core.on_account(s, account(balance=0.0), at("09:33", D3).ts)[0]
    s, _ = core.on_bar(s, at("09:30", D4))
    assert s.nt_peak == 2200.0


def test_ninjatraders_own_peak_rises_with_an_intraday_raise_the_app_takes_too():
    """The intraday side: the gap at 52,200, then a manual +1,500 both book with NinjaTrader's
    equity at 52,500 (+200 of it open: no gap, the app takes the raise to 2,500); -2,050 it alone
    books (50,450) is through its own floor 50,500."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    gap = s.halted
    s, _ = manual_loss(reconciled(s), -1500.0, day=D1)
    now = at("09:32").ts
    s, _ = core.on_account(s, account(balance=52_300.0, today=2300.0, unrealized=200.0), now)
    assert (s.acct.peak, s.acct.floor, s.nt_peak) == (2500.0, 500.0, 2500.0)  # its equity
    s, effects = core.on_account(s, account(balance=50_450.0, today=450.0), now)
    caught = nt_floor(50_450.0, floor=50_500.0, peak=52_500.0)
    assert alerts(effects) == [f"{caught} (still halted: {gap})"]


def held_over_the_close(gain, loss, nt_balance, unrealized, **rules):
    """The app books `gain` in fills, then adopts NinjaTrader's long with a known stop (19,000)
    and marks it `loss` open; the link drops over the close and NinjaTrader reports `nt_balance`
    and `unrealized` (`rules`: `sim`'s). Returns the state after that and the last bar's close."""
    s = reconciled(core.start(sim(**rules), T.ts)[0])
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(seconds=5), qty=1))
    exit_ = {**fill_at(T.ts + timedelta(seconds=10), qty=-1), "price": 20000.0 + gain / 20}
    s, _ = core.on_fill(s, exit_)
    s, _ = core.on_bar(s, at("09:31"))  # the exit's own bar
    assert (s.acct.balance, s.position.qty, s.halted) == (gain, 0, None)
    now = T.ts + timedelta(minutes=2)
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20000.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    assert (s.position.qty, s.position.stop) == (1, 19000.0)
    close = 20000.0 - loss / 20
    s, _ = core.on_bar(s, bar("09:32", 20000.0, 20000.0, close, close))
    assert (s.acct.open_pnl, s.halted) == (-loss, None)
    s, _ = core.on_disconnect(s, "stream closed")
    s, _ = core.on_account(s, account(nt_balance, today=gain, unrealized=unrealized), now)
    return s, close


def sends(effects):
    return [type(e.action) for e in effects if isinstance(e, core.Send)]


# NinjaTrader's equity 50,100 (through its floor), 50,200 (at it), 50,210 and 52,200 (above it)
@pytest.mark.parametrize("unrealized", [-2100.0, -2000.0, -1990.0, 0.0])
def test_an_open_loss_held_over_the_close_breaches_at_the_roll(unrealized):
    """Review of #221, P1: the app and NinjaTrader agree at 52,200 (no gap), and a long with a
    known stop is held unverified over the close at -2,100 open; NinjaTrader's floor is 50,200.
    Whatever open P&L NinjaTrader shows, the app's own close raises its floor from its own
    balance, as NinjaTrader's raises it from NinjaTrader's (DEC-4a), so the loss breaches and is
    flattened at the roll, as on main. With equal balances, NinjaTrader showing a smaller loss or
    none (its exit filled, or its figure is stale) cannot be told from a misreport: fail closed."""
    s, close = held_over_the_close(2200.0, 2100.0, 52_200.0, unrealized)
    s, effects = core.on_bar(s, bar("09:30", close, close, close, close, D2))
    assert s.halt_kind == "breach"
    assert s.halted == "breach max_drawdown: equity 100.00 at or below drawdown floor 200.00"
    assert sends(effects) == [Flatten]


def test_a_gap_halt_flattens_the_position_and_its_loss_still_breaches():
    """Review of #221, C5: a commission-sized gap (the app 52,150, NinjaTrader 52,200) and the
    same long held over the close at -1,960, which NinjaTrader reports accurately (its equity
    50,240, $40 above its floor). The gap halts, and a halted account the app cannot trust holds
    nothing: it is flattened at once. Its floor is raised from its own balance (50,150), so the
    loss breaches as soon as it runs through it, not at the app's old floor 48,190."""
    s, close = held_over_the_close(2150.0, 1960.0, 52_200.0, -1960.0)
    s, effects = core.on_bar(s, bar("09:30", close, close, close, close, D2))
    assert s.halted.startswith(
        "confirm balances: a gap at the end-of-day session close: NinjaTrader's balance 52200.00"
        " puts the drawdown floor at 50200.00, at or above the app's equity 50190.00 "
    )
    assert (s.halt_kind, s.acct.floor) == (None, 150.0)
    assert sends(effects) == [Flatten] and s.exiting is not None
    worse = 20000.0 - 2300 / 20  # -2,300 open: equity -150
    s, effects = core.on_bar(s, bar("09:31", worse, worse, worse, worse, D2))
    assert s.halt_kind == "breach"
    assert s.halted == "breach max_drawdown: equity -150.00 at or below drawdown floor 150.00"


@pytest.mark.parametrize(
    ("unrealized", "breach"),
    [(-1900.0, False), (-1999.99, False), (-1999.996, True), (-2000.0, True)],
)
def test_ninjatraders_own_open_pnl_decides_a_gap_against_a_flat_app(unrealized, breach):
    """Review of #221, C3: the app flat at 0, NinjaTrader at 52,200 (floor 50,200) holding a loss
    the app knows nothing of. Above its floor, to the cent, it is the gap's halt, and the account
    is flattened once (#229); at or through it, the roll takes the raise and the bar breaches."""
    s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=unrealized))
    s, effects = core.on_bar(s, at("09:30", D2))
    if breach:
        assert s.halt_kind == "breach" and s.acct.floor == 200.0
    else:
        assert s.halted.startswith("confirm balances: a gap at the end-of-day session close: ")
        assert (s.halt_kind, s.acct.floor, sends(effects)) == (None, -2000.0, [Flatten])


@pytest.mark.parametrize(("balance", "gap"), [(50_200.004, True), (50_200.01, False)])
def test_the_apps_equity_against_ninjatraders_floor_is_compared_to_the_cent(balance, gap):
    """At the floor to the cent is a breach for the rules, so it is the gap's halt; a cent above
    it, NinjaTrader's floor is taken (stricter) and nothing halts."""
    setup = make_setup(
        make_rules(),
        mode="sim",
        start=Start(50_000.0, 50_000.0, balance),
        day_start=DayStart(balance, 50_000.0),
    )
    s = running(setup, account(balance=52_200.0))
    s, _ = core.on_bar(s, at("09:30", D2))
    assert s.halt_kind is None
    if gap:
        assert s.halted.startswith("confirm balances: a gap at the end-of-day session close: ")
    else:
        assert s.halted is None and s.acct.floor == 200.0


def actions(effects):
    return [e.payload for e in effects if isinstance(e, Log) and e.kind == "action"]


# #229: under the gap halt, NinjaTrader's open P&L on a flat app is a position the app does not
# know of (a manual trade on another instrument): one account-wide /flatten, never a storm


def test_a_gap_halt_flattens_the_whole_account_while_ninjatrader_shows_open_pnl():
    """Review of #221 (round 3), non-blocking 1: the app flat at 0, NinjaTrader at 52,200 with
    -1,900 open (its floor 50,200). The roll halts and flattens the account once, journalled
    with the halt's reason; while NinjaTrader's loss runs to -3,500 the flatten is out, so
    nothing more is sent; once NinjaTrader shows no open P&L, nothing is sent either."""
    s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=-1900.0))
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith("confirm balances: a gap at the end-of-day session close: ")
    assert sends(effects) == [Flatten] and s.position.qty == 0
    [row] = actions(effects)
    assert (row["type"], row["sent"], row["reason"]) == ("Flatten", True, s.halted)
    for n, loss in enumerate((-2100.0, -2500.0, -3500.0), start=1):
        s, _ = core.on_account(s, account(52_200.0, today=0.0, unrealized=loss), T.ts)
        s, effects = core.on_bar(s, at(f"09:3{n}", D2))
        assert sends(effects) == [] and s.halt_kind is None
    s, effects = core.on_account(s, account(52_200.0 - 3500.0), T.ts)  # flattened: no open P&L
    assert s.exiting is None and effects == ()
    assert sends(core.on_bar(s, at("09:34", D2))[1]) == []


def test_a_gap_halt_on_a_flat_account_with_no_open_pnl_at_ninjatrader_sends_nothing():
    s = running(sim(), account(balance=52_200.0, today=2200.0))
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith("confirm balances: a gap ") and sends(effects) == []


def test_the_gap_flatten_of_a_held_position_is_journalled_with_the_halts_reason():
    """Review of #221 (round 3), nit K6: not "no known stop" (`_stopless`), but the halt."""
    s, close = held_over_the_close(2150.0, 1960.0, 52_200.0, -1960.0)
    s, effects = core.on_bar(s, bar("09:30", close, close, close, close, D2))
    assert [(a["type"], a["reason"]) for a in actions(effects)] == [("Flatten", s.halted)]
    assert "alert" not in kinds(effects)


# #222: an intraday peak raise from NinjaTrader's equity (every account message) that alone
# would breach the app's own figures is a gap, as at the end-of-day roll (#219)

# the clause of every breach alert that replaces a gap halt (review of #238)
NOT_THE_GAP = ", a loss through the floor the runner checks, not the gap alone"
GAP_INTRADAY = "confirm balances: a gap on NinjaTrader's account figures: NinjaTrader's equity "


def test_an_intraday_gain_the_app_never_saw_is_no_breach():
    """The issue's probe: the app at 50,000, NinjaTrader at 52,200 (a gain the app never booked):
    its raise puts the floor at 50,200, above the app's equity. That used to be `breach
    max_drawdown: equity 0.00 at or below drawdown floor 200.00` on the next bar."""
    s = running(sim(trailing="intraday"))
    s, effects = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    assert s.halted == (
        GAP_INTRADAY + "52200.00 puts the drawdown floor at 50200.00, at or above the app's"
        " equity 50000.00 (P&L the app never booked: a trade on another instrument, or a fill"
        " while the server was down); any position the app holds is flattened, and the whole"
        " account while NinjaTrader shows open P&L; re-arm with the confirmed balances"
    )
    assert (s.halt_kind, s.halt_day, s.acct.peak, s.acct.floor) == (None, None, 0.0, -2000.0)
    # the app's own peak did not move; NinjaTrader's goes in the "peak" row next to it (#225)
    assert peaks(effects) == [{"peak": 50_000.0, "ts": iso(T.ts), "nt_peak": 52_200.0}]
    s, effects = core.on_bar(s, at("09:31"))
    assert s.halt_kind is None and sends(effects) == []  # flat, and NinjaTrader holds nothing
    s, effects = core.on_account(s, account(balance=52_300.0), T.ts)  # every message: no raise
    assert s.acct.floor == -2000.0 and peaks(effects)[0]["nt_peak"] == 52_300.0


@pytest.mark.parametrize(
    ("balance", "gap"), [(52_000.0, True), (51_999.996, True), (51_999.99, False)]
)
def test_an_intraday_raise_against_the_apps_equity_is_compared_to_the_cent(balance, gap):
    """NinjaTrader's floor at the app's equity 0 (to the cent) is the gap's halt; a cent below,
    the raise is taken (stricter) and nothing halts."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=balance), T.ts)
    if gap:
        assert s.halted.startswith(GAP_INTRADAY) and s.acct.floor == -2000.0
    else:
        assert s.halted is None and s.acct.floor == pytest.approx(-0.01)


def test_a_real_loss_through_ninjatraders_intraday_floor_still_breaches():
    """A smaller gap takes the raise (floor -100, from NinjaTrader's 51,900); a loss the app
    books through it is a breach."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=51_900.0), T.ts)
    assert (s.halted, s.acct.floor) == (None, -100.0)
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19990.0, 19990.0))
    assert s.halted == "breach max_drawdown: equity -200.00 at or below drawdown floor -100.00"


@pytest.mark.parametrize("unrealized", [-1500.0, 0.0])
def test_equal_intraday_balances_with_a_stale_open_pnl_still_breach(unrealized):
    """The app and NinjaTrader agree at 52,200 and the app holds a long at -1,500 (floor 200
    from its own intraday peak). NinjaTrader's open P&L, accurate or a stale 0, raises nothing,
    so nothing halts, and the loss breaches at -2,100 and is flattened."""
    s, close = held_over_the_close(2200.0, 1500.0, 52_200.0, unrealized, trailing="intraday")
    assert (s.halted, s.acct.peak, s.acct.floor) == (None, 2200.0, 200.0)
    worse = 20000.0 - 2100 / 20
    s, effects = core.on_bar(s, bar("09:33", worse, worse, worse, worse))
    assert s.halted == "breach max_drawdown: equity 100.00 at or below drawdown floor 200.00"
    assert sends(effects) == [Flatten]


def test_an_intraday_gap_halt_flattens_a_held_position_and_its_loss_still_breaches():
    """The app at +1,000 (its intraday peak) holds a long at -500; NinjaTrader at 53,500 with the
    same -500 open (its equity 53,000, floor 51,000) is above the app's equity 50,500. The gap
    halts, the app's floor stays its own (49,000), the message flattens (#235), and a loss
    through that floor still breaches."""
    s, close = held_over_the_close(1000.0, 500.0, 53_500.0, -500.0, trailing="intraday")
    assert s.halted.startswith(GAP_INTRADAY + "53000.00 puts the drawdown floor at 51000.00, ")
    assert (s.halt_kind, s.acct.peak, s.acct.floor) == (None, 1000.0, -1000.0)
    assert s.exiting is not None  # flattened on the message that raised it (#235)
    s, effects = core.on_bar(s, bar("09:33", close, close, close, close))
    assert s.halt_kind is None and sends(effects) == []
    worse = 20000.0 - 2000 / 20  # -2,000 open: equity -1,000
    s, effects = core.on_bar(s, bar("09:34", worse, worse, worse, worse))
    assert s.halted == "breach max_drawdown: equity -1000.00 at or below drawdown floor -1000.00"


def test_an_intraday_gap_halt_flattens_the_whole_account_while_ninjatrader_shows_open_pnl():
    """The app flat at 0; NinjaTrader at 54,500 with -1,900 open (its equity 52,600, floor
    50,600): the gap halt and one account-wide flatten on that message (#235), none more."""
    s = running(sim(trailing="intraday"))
    s, effects = core.on_account(s, account(balance=54_500.0, unrealized=-1900.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY + "52600.00 puts the drawdown floor at 50600.00, ")
    assert sends(effects) == [Flatten] and actions(effects)[0]["reason"] == s.halted
    assert sends(core.on_bar(s, at("09:31"))[1]) == []
    s, _ = core.on_account(s, account(balance=54_500.0, unrealized=-3500.0), T.ts)
    assert sends(core.on_bar(s, at("09:32"))[1]) == []


def test_a_same_day_intraday_restart_after_a_gain_the_app_never_saw_is_no_breach():
    """The server was down while NinjaTrader booked +2,200 the app never saw. The restart opens
    today as journalled (50,000) and replays to the app's balance 0, so NinjaTrader's 52,200 is
    no peak at start: the account message after it is the gap's halt, not a breach."""
    setup = replace(
        sim(trailing="intraday"), start=replace(START, balance=52_200.0, realized_today=2200.0)
    )
    s, effects = core.start(replace(setup, day_start=None), T.ts, opens=[opened(D1, 50_000.0)])
    assert (s.acct.peak, s.acct.floor) == (0.0, -2000.0) and "peak" not in kinds(effects)
    s = reconciled(core.replay(s, []))
    assert s.acct.balance == 0.0
    s, _ = core.on_account(s, account(balance=52_200.0, today=2200.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY)
    s, _ = core.on_bar(s, at("09:31"))
    assert s.halt_kind is None


def test_an_intraday_restart_with_no_open_journalled_today_still_starts_at_the_balance():
    """A first start today opens at NinjaTrader's balance, which is then the app's too: the
    intraday peak starts at least there, as before."""
    s, _ = first_start(52_200.0, 2200.0)
    assert s.acct.peak == 0.0  # end_of_day (sim()): only the close raises
    setup = replace(
        sim(trailing="intraday"), start=replace(START, balance=52_200.0, realized_today=2200.0)
    )
    s, effects = core.start(replace(setup, day_start=None), T.ts)
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (2200.0, 2200.0, 200.0)
    assert peaks(effects) == [{"peak": 52_200.0, "ts": iso(T.ts)}]


def test_a_gap_halt_flattens_the_account_for_an_open_gain_at_ninjatrader_too():
    """Any open P&L the app cannot account for goes, a gain as well as a loss (#229)."""
    s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=300.0))
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith("confirm balances: a gap ") and sends(effects) == [Flatten]


def test_open_pnl_at_ninjatrader_flattens_nothing_without_a_gap_halt():
    s = reconciled(core.start(sim(), T.ts)[0])
    s, _ = core.on_account(s, account(unrealized=-500.0), T.ts)
    s, effects = core.on_bar(s, T)
    assert (s.halted, s.exiting, sends(effects)) == (None, None, [])


def test_an_intraday_gap_counts_the_apps_open_loss():
    """The app at 0 holds a long at -500; NinjaTrader at 52,200 with the same -500 open puts
    the floor at -300: above the app's equity (-500), though not its balance (0). The equity is
    what the rules check, so it is the gap's halt, not a breach on the next bar."""
    s, close = held_over_the_close(0.0, 500.0, 52_200.0, -500.0, trailing="intraday")
    assert s.halted.startswith(
        GAP_INTRADAY + "51700.00 puts the drawdown floor at 49700.00, at or above the app's"
        " equity 49500.00 ("
    )
    s, _ = core.on_bar(s, bar("09:33", close, close, close, close))
    assert s.halt_kind is None


def test_an_intraday_gap_halt_raises_the_apps_peak_to_its_own_balance():
    """A gain the app booked with no bar since (balance 1,000, peak still 0) and a larger gap at
    NinjaTrader (54,000): the halt keeps the app's figures with its peak at least its balance,
    as an intraday peak always is, so its floor is -1,000, not -2,000."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    exit_ = {**fill_at(T.ts + timedelta(minutes=1, seconds=10), qty=-1), "price": 20050.0}
    s, _ = core.on_fill(s, exit_)
    assert s.acct.peak == 0.0
    s, effects = core.on_account(s, account(balance=54_000.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY + "54000.00 puts the drawdown floor at 52000.00, ")
    assert (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)
    assert peaks(effects) == [{"peak": 51_000.0, "ts": iso(T.ts), "nt_peak": 54_000.0}]


def test_an_app_loss_through_its_own_floor_is_a_breach_not_a_gap():
    """A loss the app booked through its own floor before the next bar, then a raise from
    NinjaTrader: it is the app's own breach, not NinjaTrader's raise alone, so no gap halt."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    exit_ = {**fill_at(T.ts + timedelta(minutes=1, seconds=10), qty=-1), "price": 19895.0}
    s, _ = core.on_fill(s, exit_)
    assert s.acct.balance == -2100.0
    s, effects = core.on_account(s, account(balance=50_500.0), T.ts)
    assert s.halted is None and "halt" not in kinds(effects)
    s, _ = core.on_bar(s, at("09:31"))
    assert s.halt_kind == "breach"


def gap_flattening(trailing):
    """A gap halt with the app flat while NinjaTrader shows -1,900 open: the account-wide
    Flatten is out (the add-on accepted it, nothing filled). Intraday: NinjaTrader at 54,500
    (#222); end_of_day: 52,200 at the close (#219). Returns the state and the next fill's time."""
    if trailing == "intraday":
        s = running(sim(trailing="intraday"))
        s, effects = core.on_account(s, account(balance=54_500.0, unrealized=-1900.0), T.ts)
        s, more = core.on_bar(s, at("09:31"))  # flattened on the message (#235): not again
        effects += more
        ts = T.ts + timedelta(minutes=2, seconds=5)
    else:
        s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=-1900.0))
        s, effects = core.on_bar(s, at("09:30", D2))
        ts = at("09:30", D2).ts + timedelta(minutes=1, seconds=5)
    assert s.halted.startswith("confirm balances: a gap ") and sends(effects) == [Flatten]
    assert s.exiting is not None and s.position.qty == 0
    return s, ts


@pytest.mark.parametrize("trailing", ["intraday", "end_of_day"])
def test_a_position_booked_while_the_gap_flatten_is_out_is_flattened_at_once(trailing):
    """Review of #231, blocking 1: a fill on the app's instrument while the account-wide Flatten
    is out (a manual MNQ buy while the add-on closes a manual ES) opens a position with no known
    stop. That Flatten was sent while the app was flat, so it cannot be closing this one: the
    fill ends it and the position gets its own Flatten at once, as on main; one at a time after."""
    s, ts = gap_flattening(trailing)
    first = s.exiting
    s, effects = core.on_fill(s, fill_at(ts, qty=1))
    assert s.position.qty == 1 and sends(effects) == [Flatten]
    [row] = actions(effects)
    assert row["reason"] == "position 1 has no known stop" and s.exiting not in (None, first)
    for n, close in enumerate((19950.0, 19900.0, 19850.0), start=1):  # through the floor
        s, _ = core.on_account(s, account(54_500.0, unrealized=-5000.0), ts)
        s, effects = core.on_bar(s, bar("10:0" + str(n), close, close, close, close, ts.date()))
        assert sends(effects) == []  # its Flatten is out: no storm
    assert s.halt_kind == "breach"


def test_a_bracketed_entry_filled_while_the_gap_flatten_is_out_is_flattened_at_once():
    """The same for an entry with a known stop, sent before the gap halt and filled while the
    account-wide Flatten is out: the fill ends that Flatten (which may not close it), so it gets
    its own at once, not on the next bar, which a feed outage may hold back (#237)."""
    s = running(sim(trailing="intraday"))
    s, effects = decide_now(s, [LONG])
    assert sends(effects) == [Bracket]
    s, effects = core.on_account(s, account(balance=54_500.0, unrealized=-1900.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY) and sends(effects) == [Flatten]
    gap_flatten = s.exiting
    s, effects = core.on_bar(s, at("09:31"))
    assert sends(effects) == []
    entry = {
        **fill_at(T.ts + timedelta(minutes=2, seconds=5), qty=1),
        "client_id": s.working.client_id,
    }
    s, effects = core.on_fill(s, entry)
    assert (s.position.qty, s.position.stop, sends(effects)) == (1, 19990.0, [Flatten])
    assert [a["reason"] for a in actions(effects)] == [
        f"position 1 filled while Flatten {gap_flatten} was out"
    ]
    s, effects = core.on_bar(s, at("09:32"))
    assert sends(effects) == []  # one at a time


def test_a_same_day_intraday_restart_starts_the_peak_at_least_at_the_replayed_balance():
    """Review of #231, non-blocking 1: a gain the app booked raises an intraday peak only at the
    next Mark or account message, so a restart in between finds no "peak" row for it. Today's
    open 50,000 is journalled; the replay books +1,000 (exit at 20,050) and a re-entry at 20,050
    with no Mark since; NinjaTrader is at 51,000 with -800 open. The replayed balance holds no
    P&L the app never saw, so the peak starts there (floor -1,000), and a -2,100 open loss
    (equity -1,100) breaches it, as on main."""
    setup = replace(
        sim(trailing="intraday"), start=replace(START, balance=51_000.0, realized_today=1000.0)
    )
    s, effects = core.start(replace(setup, day_start=None), T.ts, opens=[opened(D1, 50_000.0)])
    assert (s.acct.peak, s.acct.floor) == (0.0, -2000.0) and "peak" not in kinds(effects)
    exit_ = {**fill_at(T.ts + timedelta(seconds=10), qty=-1), "price": 20050.0}
    again = {**fill_at(T.ts + timedelta(seconds=15), qty=1), "price": 20050.0}
    rows = [("fill", fill_at(T.ts + timedelta(seconds=5), qty=1)), ("fill", exit_), ("fill", again)]
    s = core.replay(s, rows)
    assert (s.acct.balance, s.position.qty, s.acct.peak, s.acct.floor) == (
        1000.0,
        1,
        1000.0,
        -1000.0,
    )
    now = T.ts + timedelta(seconds=30)
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20050.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    s, _ = core.on_account(s, account(51_000.0, today=1000.0, unrealized=-800.0), now)
    assert (s.halted, s.acct.peak, s.acct.floor) == (None, 1000.0, -1000.0)
    worse = 20050.0 - 2100 / 20
    s, _ = core.on_bar(s, bar("09:31", worse, worse, worse, worse))
    assert s.halted == "breach max_drawdown: equity -1100.00 at or below drawdown floor -1000.00"


def test_a_dry_replay_raises_no_peak():
    """Dry runs have no NinjaTrader start, so no phase start to raise the peak from."""
    setup = make_setup(make_rules(trailing="intraday"))
    assert setup.start is None
    s, _ = core.start(setup, T.ts)
    exit_ = {**fill_at(T.ts + timedelta(seconds=10), qty=-1), "price": 20050.0}
    s = core.replay(s, [("fill", fill_at(T.ts + timedelta(seconds=5), qty=1)), ("fill", exit_)])
    assert s.acct.peak == 0.0


def alerts(effects):
    return [e.payload["message"] for e in effects if isinstance(e, Log) and e.kind == "alert"]


def test_a_same_bar_breach_after_an_intraday_gap_halt_is_not_put_down_to_the_gap():
    """Review of #231, nit: the app holds a long marked at 0 when NinjaTrader's +2,100 open (a
    spike within the bar) puts its floor at 100, at or above the app's equity: the gap's halt.
    The bar's own high-first Mark then breaches on the app's own figures, which replaces it; the
    alert says so, not that the breach followed a gap."""
    s = reconciled(core.start(sim(trailing="intraday"), T.ts)[0])
    now = T.ts + timedelta(seconds=30)
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20000.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    s, _ = core.on_bar(s, bar("09:30", 20000.0, 20000.0, 20000.0, 20000.0))
    s, _ = core.on_account(s, account(50_000.0, unrealized=2100.0), now)
    gap = s.halted
    assert gap.startswith(GAP_INTRADAY + "52100.00 puts the drawdown floor at 50100.00, ")
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20105.0, 20000.0, 20100.0))
    breach = "breach max_drawdown: equity 0.00 at or below drawdown floor 100.00"
    assert (s.halted, s.halt_kind) == (breach, "breach")
    assert alerts(effects) == [f"{breach}{NOT_THE_GAP} (was halted: {gap})"]


def test_a_breach_that_replaces_another_open_ended_halt_alerts_with_its_reason():
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(is_sim=False), T.ts)
    was = s.halted
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19895.0, 19895.0))
    breach = "breach max_drawdown: equity -2100.00 at or below drawdown floor -2000.00"
    assert s.halted == breach and alerts(effects)[0] == f"{breach} (was halted: {was})"


# review of #231: its mutants R2-R8, which the tests above let through


@pytest.mark.parametrize("halt", ["strategy error", "safety margin"])
def test_open_pnl_at_ninjatrader_flattens_nothing_under_another_halt(halt):
    """R7, R8: only a gap halt flattens a flat app's account; another halt, open-ended (a
    strategy error) or for the day (the safety margin), leaves NinjaTrader's open P&L alone."""
    balance = 48_500.0 if halt == "safety margin" else 50_000.0  # the safety level: -1,600
    s = running(sim(), account(balance=balance, unrealized=-150.0))
    if halt == "strategy error":
        s, _ = core.step_failed(s, "boom")
    assert s.halted.startswith(halt) and s.position.qty == 0
    s, effects = core.on_bar(s, at("09:31"))
    assert (s.exiting, sends(effects)) == (None, [])


def test_the_gap_flatten_waits_for_ninjatraders_figures_after_a_lost_link():
    """R3: with no NinjaTrader figures (the link lost, then a reconcile that ends the account-wide
    Flatten), a flat app sends nothing; NinjaTrader's next figures with open P&L flatten again."""
    s, ts = gap_flattening("intraday")
    s, _ = core.on_disconnect(s, "stream closed")
    s, _ = core.on_reconcile(s, INSTRUMENT, [], [], ts)
    assert (s.nt, s.exiting, s.position.qty) == (None, None, 0)
    s, effects = core.on_bar(s, at("09:32"))
    assert sends(effects) == []
    s, _ = core.on_account(s, account(balance=54_500.0, unrealized=-1900.0), ts)
    s, effects = core.on_bar(s, at("09:33"))
    assert sends(effects) == [Flatten]


def test_a_stale_zero_open_pnl_never_ends_the_gap_flatten_of_a_held_position():
    """R2: the app holds the long its gap Flatten is closing; NinjaTrader then shows no open P&L
    (a stale figure, the app still long): that Flatten stays out, so nothing more is sent."""
    s, close = held_over_the_close(2150.0, 1960.0, 52_200.0, -1960.0)
    s, effects = core.on_bar(s, bar("09:30", close, close, close, close, D2))
    assert sends(effects) == [Flatten]
    s, _ = core.on_account(s, account(52_200.0), T.ts)
    assert s.exiting is not None
    s, effects = core.on_bar(s, bar("09:31", close, close, close, close, D2))
    assert sends(effects) == []


def test_an_intraday_gap_halt_raises_the_peak_to_the_balance_not_the_equity():
    """R4: the app booked +1,000 (exit at 20,050), re-entered at 20,050 and was marked between
    -200 and -400 (close -300): its peak 800. The gap halt raises it to the balance, 1,000, as
    an intraday peak never stays below the balance, not to its equity, 700."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    exit_ = {**fill_at(T.ts + timedelta(minutes=1, seconds=10), qty=-1), "price": 20050.0}
    again = {**fill_at(T.ts + timedelta(minutes=1, seconds=15), qty=1), "price": 20050.0}
    s, _ = core.on_fill(s, exit_)
    s, _ = core.on_fill(s, again)
    s, _ = core.on_bar(s, bar("09:31", 20035.0, 20040.0, 20030.0, 20035.0))
    assert (s.acct.balance, s.acct.open_pnl, s.acct.peak) == (1000.0, -300.0, 800.0)
    s, _ = core.on_account(s, account(balance=53_500.0, unrealized=-300.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY + "53200.00 puts the drawdown floor at 51200.00, ")
    assert (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)


def test_a_same_day_intraday_restart_starts_from_the_highest_journalled_peak():
    """R6: today's open journalled at peak 50,000, then a raise to 51,000 journalled as a "peak"
    row: a same-day restart starts from 51,000 (floor -1,000), not the open's peak."""
    setup = replace(
        sim(trailing="intraday"), start=replace(START, balance=52_200.0, realized_today=2200.0)
    )
    peak = [{"peak": 51_000.0, "ts": iso(T.ts)}]
    opens = [opened(D1, 50_000.0)]
    s, _ = core.start(replace(setup, day_start=None), T.ts, peaks=peak, opens=opens)
    assert (s.acct.peak, s.acct.floor) == (1000.0, -1000.0)


def test_a_same_day_intraday_restart_raises_the_peak_to_the_balance_kept_not_the_booked_one():
    """The replay books +1,000 but NinjaTrader is at 50,500 (a loss the journal never saw): the
    balance kept is NinjaTrader's lower one, and the peak rises to it, 500, not to 1,000."""
    setup = replace(
        sim(trailing="intraday"), start=replace(START, balance=50_500.0, realized_today=500.0)
    )
    s, _ = core.start(replace(setup, day_start=None), T.ts, opens=[opened(D1, 50_000.0)])
    exit_ = {**fill_at(T.ts + timedelta(seconds=10), qty=-1), "price": 20050.0}
    s = core.replay(s, [("fill", fill_at(T.ts + timedelta(seconds=5), qty=1)), ("fill", exit_)])
    assert (s.acct.balance, s.acct.peak, s.acct.floor) == (500.0, 500.0, -1500.0)


# #235: an intraday gap, or a gap at the end-of-day roll, under another open-ended halt that is
# not a breach (strategy error, live runner error, warm-up diverged, a mode mismatch): the gap
# halt replaces it, so it is journalled, alerted and flattened, on the gap halt's own floor


def held_long(trailing="intraday"):
    """A bracketed long (stop 19,000) adopted from NinjaTrader and marked at 0 (the probe's)."""
    s = reconciled(core.start(sim(trailing=trailing), T.ts)[0])
    now = T.ts + timedelta(seconds=30)
    held = [NtPosition("Sim101", INSTRUMENT, 1, 20000.0)]
    stop = [NtOrder("Sim101", "o1", "x", INSTRUMENT, "stop", "sell", 1, 19000.0, "working")]
    s, _ = core.on_reconcile(s, INSTRUMENT, held, stop, now)
    s, _ = core.on_bar(s, bar("09:30", 20000.0, 20000.0, 20000.0, 20000.0))
    return s


DIVERGED = "warm-up diverged at 2025-10-06T13:30:00+00:00: journalled [], replayed []"
OTHER_HALTS = {
    "strategy error": lambda s: core.step_failed(s, "boom")[0],
    "live runner error": lambda s: core.halt(s, "live runner error: KeyError: 'x'")[0],
    "warm-up diverged": lambda s: core.halt(s, DIVERGED)[0],
    "mode mismatch": lambda s: core.on_account(s, account(is_sim=False), T.ts)[0],
}


@pytest.mark.parametrize("other", OTHER_HALTS)
def test_an_intraday_gap_under_another_open_ended_halt_replaces_it(other):
    """The review of #231 (round 2) probe: a bracketed long marked at 0, then another halt, then
    NinjaTrader at 52,100 (a +2,100 gain the app never saw). The gap halt replaces the other:
    one "halt" row, an alert naming the halt it replaced, and the long is flattened with the
    gap's reason (by the next bar at the latest). The figures are the gap halt's own, exactly as
    without the other halt: the app's room to its floor (2,000) is NinjaTrader's (52,100 less
    its floor 50,100), never more."""
    s = OTHER_HALTS[other](held_long())
    was = s.halted
    assert was.startswith(other) and s.halt_kind is None
    gap_account = account(balance=52_100.0, today=2100.0)
    s, effects = core.on_account(s, gap_account, T.ts)
    assert s.halted.startswith(GAP_INTRADAY + "52100.00 puts the drawdown floor at 50100.00, ")
    assert (s.halt_kind, s.halt_day) == (None, None)
    halts = [e.payload for e in effects if isinstance(e, Log) and e.kind == "halt"]
    assert halts == [{"reason": s.halted, "kind": None, "until": None}]
    assert alerts(effects) == [f"{s.halted} (was halted: {was})"]
    plain, _ = core.on_account(held_long(), gap_account, T.ts)
    assert s.acct == plain.acct and (s.acct.peak, s.acct.floor) == (0.0, -2000.0)
    sent = sends(effects)
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 20000.0, 20000.0))
    assert sent + sends(effects) == [Flatten] and s.halted == plain.halted
    assert [a["reason"] for a in actions(effects)] in ([], [s.halted])


@pytest.mark.parametrize(
    "changed", [{"strategy_hash": "h2"}, {"rules_hash": "r2"}], ids=["strategy", "rules"]
)
def test_an_intraday_gap_never_replaces_a_refused_start(changed):
    """Review of #236, non-blocking 1: a start refused because a pinned input changed since
    arming (`started=False`) keeps its own halt. Its figures are not the account's (they come
    from inputs that may have been edited since arming, and the status shows none of them), so
    no gap is read from them and nothing is flattened on them. The account stays halted until
    re-armed either way, and the refusal's reason is the one that says what to do."""
    s, effects = core.start(replace(sim(trailing="intraday"), **changed), T.ts)
    refused = s.halted
    assert not s.started and refused.endswith("changed since arming; re-arm")
    gap_account = account(balance=52_100.0, today=2100.0, unrealized=-50.0)
    s, effects = core.on_account(reconciled(s), gap_account, T.ts)
    assert (s.halted, s.halt_kind, s.exiting) == (refused, None, None)
    assert "halt" not in kinds(effects) and sends(effects) == []
    s, effects = core.on_bar(s, at("09:31"))
    assert s.halted == refused and sends(effects) == []


@pytest.mark.parametrize("other", ["strategy error", "warm-up diverged"])
def test_a_gap_at_the_roll_under_another_open_ended_halt_replaces_it(other):
    """The same at an end_of_day roll (#219): the app flat at 0, NinjaTrader closes at 52,200
    with -1,900 open, under another halt: the gap halt, alerted, and the account flattened."""
    s = running(sim(), account(balance=52_200.0, today=2200.0, unrealized=-1900.0))
    s = OTHER_HALTS[other](s)
    was = s.halted
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted.startswith("confirm balances: a gap at the end-of-day session close: ")
    assert alerts(effects) == [f"{s.halted} (was halted: {was})"]
    assert sends(effects) == [Flatten] and actions(effects)[0]["reason"] == s.halted


def test_a_gap_never_replaces_a_breach_or_another_gap():
    """A breach stays a breach (the gap is no downgrade), and a gap halt whose figures moved
    is not journalled again."""
    s = running(sim(trailing="intraday"))
    s, _ = core.on_fill(s, fill_at(T.ts + timedelta(minutes=1, seconds=5), qty=1))
    s, _ = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 19895.0, 20000.0))  # low -2,100
    breach = s.halted
    assert s.halt_kind == "breach" and s.acct.balance + s.acct.open_pnl == 0.0
    s, effects = core.on_account(s, account(balance=52_100.0), T.ts)  # a gap on the app's 0
    assert (s.halted, s.halt_kind) == (breach, "breach") and "halt" not in kinds(effects)
    s = running(sim(trailing="intraday"))
    s, _ = core.on_account(s, account(balance=52_200.0), T.ts)
    gap = s.halted
    s, effects = core.on_account(s, account(balance=52_500.0), T.ts)
    assert s.halted == gap and "halt" not in kinds(effects)


@pytest.mark.parametrize("gap_last", [True, False])
def test_a_restart_restores_the_gap_halt_over_another_open_ended_halt(gap_last):
    """The halt rows replay through the same rule: the gap halt holds whichever came first."""
    gap = GAP_INTRADAY + "52100.00 puts the drawdown floor at 50100.00, …"
    rows = [
        {"reason": "strategy error: boom", "kind": None, "until": None},
        {"reason": gap, "kind": None, "until": None},
    ]
    s, _ = core.start(sim(trailing="intraday"), T.ts, halts=rows if gap_last else rows[::-1])
    assert (s.halted, s.halt_kind) == (gap, None)


# #235, item 2: the intraday gap halt flattens on the account message that raises it, not only
# on the next LSE bar, which a feed outage may hold back


def test_the_intraday_gap_halt_flattens_a_held_position_on_its_own_message():
    s = held_long()
    now = T.ts + timedelta(seconds=40)
    s, effects = core.on_account(s, account(balance=52_100.0, today=2100.0), now)
    cid = core.client_id("Sim101", now, f"g{now.timestamp()}", Flatten())
    assert s.halted.startswith(GAP_INTRADAY) and s.exiting == cid
    assert [e.client_id for e in effects if isinstance(e, core.Send)] == [cid]
    assert [(a["type"], a["reason"]) for a in actions(effects)] == [("Flatten", s.halted)]
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20000.0, 20000.0, 20000.0))
    assert sends(effects) == []  # it is out: one at a time


@pytest.mark.parametrize(("unrealized", "flattens"), [(-1900.0, True), (0.0, False)])
def test_the_intraday_gap_halt_flattens_the_account_on_its_own_message(unrealized, flattens):
    """The app flat: the whole account while NinjaTrader shows open P&L, else nothing."""
    s = running(sim(trailing="intraday"))
    s, effects = core.on_account(s, account(balance=54_500.0, unrealized=unrealized), T.ts)
    assert s.halted.startswith(GAP_INTRADAY)
    assert sends(effects) == ([Flatten] if flattens else [])


def test_only_the_message_that_raises_the_gap_halt_flattens():
    """Later messages under the halt never flatten (every 2 s, a refused send would storm): after
    a refused send the next bar sends again, as before."""
    s = running(sim(trailing="intraday"))
    s, effects = core.on_account(s, account(balance=54_500.0, unrealized=-1900.0), T.ts)
    [first] = [e for e in effects if isinstance(e, core.Send)]
    s, _ = core.send_failed(s, first.client_id, "refused")
    s, effects = core.on_account(s, account(balance=54_500.0, unrealized=-2000.0), T.ts)
    assert (s.exiting, effects) == (None, ())
    s, effects = core.on_bar(s, at("09:31"))
    assert sends(effects) == [Flatten]


def test_the_gap_message_sends_no_second_flatten_while_one_is_out():
    """A Flatten already out (the strategy's, here) closes the account: none more."""
    s, effects = decide_now(held_long(), [Flatten()])
    assert sends(effects) == [Flatten] and s.exiting is not None
    s, effects = core.on_account(s, account(balance=52_100.0, today=2100.0, unrealized=-50.0), T.ts)
    assert s.halted.startswith(GAP_INTRADAY) and sends(effects) == []
