from dataclasses import replace
from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, NQ, bar, make_rules, scripted
from live_helpers import decide_now, fill, make_setup, reconciled

from fenrir.backtest import attempt
from fenrir.core.actions import Bracket, Cancel, Flatten
from fenrir.live import core
from fenrir.live.core import DayStart, Send, Start
from fenrir.rules.account import build_rules, initial_state

LONG = Bracket("long", 1, 19990.0, 20020.0)
START = Start(phase_start=50_000.0, peak=50_000.0, balance=50_000.0)
OPEN = DayStart(balance=50_000.0, peak=50_000.0)


def at(hhmm, day=D1, c=20000.0, lo=19995.0, hi=20005.0):
    return bar(hhmm, 20000.0, hi, lo, c, day)


def sim(**kw):
    return make_setup(make_rules(**kw), mode="sim", start=START, day_start=OPEN)


NT = core.NtAccount(50_000.0, 0.0, 0.0)  # NinjaTrader's figures, for an end_of_day roll


def started(setup, b):
    return core.on_bar(reconciled(core.start(setup, b.ts)[0]), b)[0]


def kinds(effects):
    return [e.kind if isinstance(e, core.Log) else "send" for e in effects]


def sends(effects):
    return [e for e in effects if isinstance(e, Send)]


def entered(setup, price=20000.25, b=None):
    """Sim account with LONG sent on `b` (09:30) and its entry filled 1 s into the next bar."""
    b = b or at("09:30")
    s, _ = decide_now(started(setup, b), [LONG])
    entry = fill(1, price, b.ts + timedelta(minutes=1, seconds=1), cid=s.working.client_id)
    return core.on_fill(s, entry)[0]


def test_client_id_is_deterministic_and_at_most_24_chars():
    ts, name = at("09:30").ts, "Lucid-Account-0001-with-a-long-name"
    a = core.client_id(name, ts, "0", LONG)
    assert a == core.client_id(name, ts, "0", replace(LONG, qty=2))  # the action's type only
    assert len(a) == 24 and a.startswith("2510061330-")
    others = {
        core.client_id("Lucid-Account-0002-with-a-long-name", ts, "0", LONG),
        core.client_id(name, ts + timedelta(minutes=1), "0", LONG),
        core.client_id(name, ts, "1", LONG),
        core.client_id(name, ts, "x", LONG),
        # a re-decided bar whose action list changed never gives a Flatten a Bracket's id
        core.client_id(name, ts, "0", Flatten()),
    }
    assert a not in others and len(others) == 5
    assert all(len(c) == 24 for c in others)


def test_dry_mode_never_sends():
    s = started(make_setup(make_rules()), at("09:30"))
    s, effects = decide_now(s, [Flatten(), Cancel("o1"), LONG])
    assert sends(effects) == [] and s.working is None
    assert [e.payload["sent"] for e in effects[1:]] == [False, False, False]


def test_sim_sends_accepted_actions_with_their_client_ids():
    b = at("09:30")
    actions = [Flatten(), Cancel("o1"), LONG]
    s, effects = decide_now(started(sim(), b), actions)
    # a Cancel is never sent: step cannot know NinjaTrader's order ids (backtest: same-bar only);
    # a Flatten while flat has nothing to close (backtest: a no-op)
    assert kinds(effects) == ["bar", "action", "action", "action", "send"]
    cid = core.client_id("Sim101", b.ts, "2", LONG)
    assert sends(effects) == [Send(LONG, cid)]
    logs = [effects[i].payload for i in (1, 2, 3)]
    assert [(p["sent"], p.get("client_id")) for p in logs] == [
        (False, None),
        (False, None),
        (True, cid),
    ]
    assert logs[0]["reason"] == "flat: nothing to close"
    assert s.working == core.Working(cid, LONG, b.close)


def test_one_flatten_at_a_time():
    s = entered(sim())
    s, effects = decide_now(core.on_bar(s, at("09:31"))[0], [Flatten(), Flatten()])
    cid = core.client_id("Sim101", at("09:31").ts, "0", Flatten())
    assert sends(effects) == [Send(Flatten(), cid)] and s.exiting == cid
    assert effects[3].payload["sent"] is False and cid in effects[3].payload["reason"]
    # on a flat-by bar the runner's own Flatten goes out and the strategy's is held back
    s = entered(sim(), b=at("16:30"))
    late = at("16:39")
    s, effects = core.on_bar(s, late)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", late.ts, "x", Flatten()))]
    s, effects = decide_now(s, [Flatten()])
    assert sends(effects) == [] and effects[1].payload["sent"] is False


def test_roll_window_bars_still_mark_and_flatten_a_position():
    # 2025-12-16 is in NQ Z25 -> H26's roll window: step never runs, risk management still does
    day = date(2025, 12, 16)
    s = started(sim(), at("09:30", day))
    s = replace(s, position=core.Position(1, 20000.0, 19990.0, 20020.0))  # e.g. adopted
    late = at("16:39", day, c=20010.0)
    s, effects = core.on_bar(s, late)
    assert s.equity[-1] == (late.ts, 200.0) and s.bars == ()
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", late.ts, "x", Flatten()))]
    assert kinds(effects)[-1] == "bar"


def test_rejected_and_replaced_brackets_are_not_sent():
    s, effects = decide_now(started(sim(), at("09:30")), [LONG, Flatten()])
    assert kinds(effects) == ["bar", "rejection", "action"]
    assert sends(effects) == [] and s.working is None  # the Flatten has nothing to close


def test_working_entry_blocks_a_second_bracket_until_it_fills():
    b0, b1 = at("09:30"), at("09:31")
    s, _ = decide_now(started(sim(), b0), [LONG])
    s, _ = core.on_bar(s, b1)  # the entry fill is late
    s2, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"] and s2.working == s.working
    assert "still working" in effects[1].payload["reason"]
    s, effects = core.on_fill(
        s, fill(1, 20000.25, b1.ts + timedelta(seconds=30), cid=s.working.client_id)
    )
    assert kinds(effects) == ["fill"] and s.working is None
    assert s.position == core.Position(1, 20000.25, 19990.0, 20020.0)
    assert decide_now(s, [LONG])[1][1].payload["reason"] == "already in a position"


def test_a_fill_that_is_not_the_entry_keeps_it_working():
    s, _ = decide_now(started(sim(), at("09:30")), [LONG])
    w = s.working
    s, _ = core.on_fill(s, fill(1, 20000.0, at("09:31").ts))  # e.g. a manual order
    assert s.working == w and s.position == core.Position(1, 20000.0, None, None)
    s, _ = core.on_fill(s, fill(-1, 20000.0, at("09:31").ts + timedelta(seconds=5)))
    assert s.working == w  # flat again, and still no second bracket until it resolves
    assert "still working" in decide_now(s, [LONG])[1][1].payload["reason"]


def test_failed_send_alerts_and_frees_the_entry():
    s, effects = decide_now(started(sim(), at("09:30")), [LONG])
    cid = sends(effects)[0].client_id
    s, effects = core.send_failed(s, cid, "unmanaged_account")
    assert kinds(effects) == ["alert"] and cid in effects[0].payload["message"]
    assert s.working is None


def test_entry_and_exit_fill_book_position_and_pnl():
    s = entered(sim(commission=1.0))
    assert (s.position.qty, s.acct.position, s.acct.balance) == (1, 1, -1.0)
    exit_ts = at("09:35").ts + timedelta(seconds=20)
    s, effects = core.on_fill(s, fill(-1, 20010.0, exit_ts, commission=0.5))
    assert kinds(effects) == ["fill"]
    assert s.position == core.FLAT and s.acct.position == 0
    # gross (20010 − 20000.25) × $20 = $195, less the rules' $1 per side (above NT's $0.50)
    assert s.acct.balance == pytest.approx(195.0 - 2.0)
    st = core.account_status(s)
    assert len(st["session"]["fills"]) == 2 and st["position"]["qty"] == 0


ENTRY_ID = core.client_id("Sim101", at("09:30").ts, "0", LONG)  # `entered`'s bracket


def test_partial_entry_fills_average_the_entry():
    """A later part of the entry carries its bracket's client_id: the add-on resizes the stop and
    target to cover it, so the bracket's stop still covers the whole position."""
    s = entered(sim())
    later = fill(1, 20001.25, at("09:31").ts + timedelta(seconds=2), cid=ENTRY_ID)
    s, effects = core.on_fill(s, later)
    assert s.position == core.Position(2, 20000.75, 19990.0, 20020.0) and sends(effects) == []


def test_an_entry_filled_one_lot_at_a_time_keeps_its_stop():
    """Review of #240: every later part of the entry keeps the stop, not only the first after
    the fill that opened the position (`entry_id` stays the bracket's through them)."""
    b = at("09:30")
    s, _ = decide_now(started(sim(), b), [replace(LONG, qty=3)])
    cid = s.working.client_id
    t = b.ts + timedelta(minutes=1, seconds=1)
    out = []
    for k in range(3):
        s, effects = core.on_fill(s, fill(1, 20000.0, t + timedelta(milliseconds=k), cid=cid))
        out += sends(effects)
    assert s.position == core.Position(3, 20000.0, 19990.0, 20020.0) and out == []


def test_a_manual_add_on_a_bracketed_position_has_no_known_stop():
    """#237: a fill that adds and is not the entry's (a manual buy) is under no stop at
    NinjaTrader, which resizes the bracket's stop and target on entry fills only. The position
    has no known stop: it is flattened at once, which also cancels that stop and target."""
    s = entered(sim())
    t = at("09:31").ts + timedelta(seconds=20)
    s, effects = core.on_fill(s, fill(1, 20001.0, t))
    assert s.position == core.Position(2, 20000.625, None, None)
    cid = core.client_id("Sim101", t, "f2", Flatten())
    assert sends(effects) == [Send(Flatten(), cid)] and s.exiting == cid
    assert reasons(effects) == ["position 2 has no known stop"]
    # later parts of the entry never bring the stop back: it covers fewer lots than are held
    s, _ = core.on_fill(s, fill(-1, 20001.0, t + timedelta(seconds=5)))
    s, _ = core.on_fill(s, fill(1, 20001.0, t + timedelta(seconds=6), cid=ENTRY_ID))
    assert (s.position.qty, s.position.stop, s.position.target) == (2, None, None)


def test_same_minute_winner_is_a_possible_microscalp():
    s = entered(sim())
    s, _ = core.on_fill(s, fill(-1, 20020.0, at("09:31").ts + timedelta(seconds=40)))
    assert s.acct.possible_microscalp_pnl == pytest.approx(19.75 * 20)


def backtest_account(bars, script, rules):
    """The backtest's account state and fills after folding every bar (attempt._STAGES)."""
    strategy = scripted(script)
    env = attempt._Env(bars, strategy, rules, NQ, rules.costs.commission_per_side.NQ, "16:40", D1)
    sim = attempt._Sim(
        initial_state(rules, "evaluation", D1),
        build_rules(rules, "evaluation"),
        strategy.init(strategy.params),
    )
    for i in range(len(bars)):
        for stage in attempt._STAGES:
            sim = stage(sim, env, i)
    return sim.account, sim.fills


@pytest.mark.parametrize(
    ("trailing", "daily_loss", "exit_bar"),
    [
        # stops out at 19990 − 1 tick after trading up to 20015 in the same bar
        ("end_of_day", None, bar("09:32", 20000.0, 20015.0, 19980.0, 19985.0)),
        ("intraday", None, bar("09:32", 20000.0, 20015.0, 19980.0, 19985.0)),
        ("intraday", None, bar("09:32", 20000.0, 20025.0, 19995.0, 20010.0)),  # target
        ("end_of_day", 500.0, bar("09:32", 20000.0, 20005.0, 19980.0, 19985.0)),
        # gaps through the stop: the realized −$412 crosses the −$300 daily loss limit
        ("end_of_day", 300.0, bar("09:32", 19980.0, 19985.0, 19975.0, 19980.0)),
        ("intraday", 300.0, bar("09:32", 19980.0, 19985.0, 19975.0, 19980.0)),
    ],
)
def test_live_fills_book_like_the_backtest(trailing, daily_loss, exit_bar):
    rules = make_rules(commission=1.0, trailing=trailing, daily_loss=daily_loss)
    bars = (at("09:30"), at("09:31"), exit_bar, at("10:00", D2))  # then the next day's first bar
    at_exit, fills = backtest_account(bars[:3], {"09:30": [LONG]}, rules)
    after_roll, _ = backtest_account(bars, {"09:30": [LONG]}, rules)
    assert len(fills) == 2
    s = started(replace(sim(), rules=rules), bars[0])
    s, _ = decide_now(s, [LONG])
    cid = s.working.client_id
    for b, f in zip(bars[1:3], fills, strict=True):
        s, _ = core.on_fill(s, fill(f.qty, f.price, f.ts + timedelta(seconds=1), cid=cid))
        s, _ = core.on_bar(s, b)
    assert s.acct == at_exit
    assert s.acct.halted_today is (daily_loss == 300.0)
    s, _ = core.on_bar(s, bars[3])
    assert s.acct == after_roll and s.acct.day == D2 and len(s.acct.closed_days) == 1


def test_daily_loss_limit_hit_flattens_once():
    s = entered(sim(daily_loss=500.0))
    deep = bar("09:31", 20000.0, 20000.0, 19990.0, 19992.0)  # worst at the stop: −$205
    s, effects = core.on_bar(s, deep)
    assert sends(effects) == []  # −$205 is above −$500
    s = replace(s, acct=replace(s.acct, balance=-300.0))  # an earlier loss today
    b = at("09:32", c=19992.0, lo=19990.0)
    s, effects = core.on_bar(s, b)
    assert s.acct.halted_today and (s.halt_kind, s.halt_day) == ("safety", D1)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", b.ts, "x", Flatten()))]
    s, effects = core.on_bar(s, at("09:33", c=19992.0, lo=19990.0))
    assert sends(effects) == []  # once
    s, _ = core.on_fill(s, fill(-1, 19991.75, at("09:34").ts + timedelta(seconds=1)))
    assert s.position == core.FLAT and s.exiting is None


def test_exit_bar_is_marked_with_the_position_held():
    # down $320 today; the bar dips $185 against the long, then fills the target: the backtest
    # marks the dip (−$505 ≤ −$500) before the exit, so the day halts despite the win
    s = entered(sim(daily_loss=500.0))
    s = replace(s, acct=replace(s.acct, balance=-320.0, day_start_balance=0.0))
    exit_ts = at("09:31").ts + timedelta(seconds=50)
    s, _ = core.on_fill(s, fill(-1, 20020.0, exit_ts))
    s, effects = core.on_bar(s, bar("09:31", 20000.0, 20025.0, 19991.0, 20010.0))
    assert s.acct.halted_today and s.halt_kind == "safety" and sends(effects) == []
    assert s.acct.open_pnl == 0.0 and s.held is None


def test_drawdown_breach_halts_and_flattens_once():
    s = entered(sim())
    s = replace(s, acct=replace(s.acct, balance=-1900.0))
    b = at("09:31", lo=19990.0)  # worst −$205 → equity −2,105 below the −2,000 floor
    s, effects = core.on_bar(s, b)
    assert (s.halt_kind, s.halt_day) == ("breach", None) and s.halted.startswith(
        "breach max_drawdown"
    )
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", b.ts, "x", Flatten()))]
    assert sends(core.on_bar(s, at("09:32"))[1]) == []


def test_flat_by_flattens_and_the_day_rolls_after_the_exit_fill():
    s = entered(sim(), b=at("16:30"))
    late = at("16:39")  # closes at 16:40
    s, effects = core.on_bar(s, at("16:38"))
    assert sends(effects) == []
    s, effects = core.on_bar(s, late)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", late.ts, "x", Flatten()))]
    assert sends(core.on_bar(s, at("16:40"))[1]) == []
    s, _ = core.on_fill(s, fill(-1, 20004.75, at("16:40").ts + timedelta(seconds=1)))
    s = replace(s, nt=NT)  # the close's balance, so the roll takes it
    s, effects = core.on_bar(s, at("18:00"))  # 18:00 ET opens trading day D2
    assert s.halted is None and "halt" not in kinds(effects)
    assert s.acct.closed_days == ((D1, pytest.approx(4.5 * 20)),)
    assert core.account_status(s)["session"]["fills"] == []


def test_a_flatten_through_zero_flattens_the_reversed_position():
    s = entered(sim(), b=at("16:30"))
    s, effects = core.on_bar(s, at("16:39"))  # flat-by: the runner's Flatten goes out
    over = at("16:40").ts + timedelta(seconds=1)
    s, effects = core.on_fill(s, fill(-2, 20004.75, over))  # closed one too many
    cid = core.client_id("Sim101", over, "f2", Flatten())  # today's second fill
    assert s.position == core.Position(-1, 20004.75, None, None)
    assert sends(effects) == [Send(Flatten(), cid)] and s.exiting == cid
    assert effects[-2].payload["reason"] == "position -1 has no known stop"
    assert effects[-3].kind == "alert" and "no known stop" in effects[-3].payload["message"]
    s, effects = core.on_bar(s, at("16:40"))
    assert sends(effects) == []  # once
    s, _ = core.on_fill(s, fill(1, 20004.75, over + timedelta(seconds=1)))
    assert s.position == core.FLAT and s.exiting is None


def stopless_opens_in_one_minute():
    """Two foreign (e.g. manual) opens in one UTC minute, each flattened; the Sends' ids."""
    s, ids = started(sim(), at("09:30")), []
    t = at("09:31").ts
    for qty, sec in [(1, 5), (-1, 10), (1, 20)]:
        s, effects = core.on_fill(s, fill(qty, 20000.0, t + timedelta(seconds=sec)))
        ids += [e.client_id for e in sends(effects)]
    return ids


def test_each_stopless_open_gets_its_own_flatten_id():
    ids = stopless_opens_in_one_minute()
    assert len(ids) == 2 and ids[0] != ids[1]  # a reused id would come back `duplicate`
    assert stopless_opens_in_one_minute() == ids  # a replay of the same fills: the same ids


def test_a_failed_flatten_of_a_stopless_position_is_sent_again():
    s = started(sim(), at("09:30"))
    s, effects = core.on_fill(s, fill(1, 20000.0, at("09:31").ts + timedelta(seconds=5)))
    [first] = sends(effects)
    s, _ = core.send_failed(s, first.client_id, "rejected")
    b = at("09:31")
    s, effects = core.on_bar(s, b)  # not late, no halt: only the missing stop
    again = core.client_id("Sim101", b.ts, "x", Flatten())
    assert sends(effects) == [Send(Flatten(), again)] and s.exiting == again
    assert "alert" in kinds(effects)


# #234: a manual add on the app's instrument while the app's Flatten is out


def reasons(effects):
    return [e.payload["reason"] for e in effects if isinstance(e, core.Log) and e.kind == "action"]


def test_a_manual_add_while_a_flatten_is_out_gets_its_own_flatten():
    """The issue's sequence: long 1 with no known stop, its Flatten out; a manual +1 fills first,
    then that Flatten's -1. The Flatten was posted for 1 lot, so the add ends it: the 2 lots get
    their own Flatten at once, and the lot left after the first Flatten's fill is never left
    unflattened (it was: `exiting` stayed the first id and nothing went out until a reconcile)."""
    s = started(sim(), at("09:30"))
    t = at("09:31").ts
    s, effects = core.on_fill(s, fill(1, 20000.0, t + timedelta(seconds=5)))
    [first] = sends(effects)
    s, effects = core.on_fill(s, fill(1, 20000.0, t + timedelta(seconds=6)))  # the manual add
    cid = core.client_id("Sim101", t + timedelta(seconds=6), "f2", Flatten())
    assert sends(effects) == [Send(Flatten(), cid)] and cid != first.client_id
    assert reasons(effects) == ["position 2 has no known stop"]
    s, effects = core.on_fill(s, fill(-1, 20000.0, t + timedelta(seconds=7)))  # the first's
    assert (s.position.qty, s.exiting, sends(effects)) == (1, cid, [])
    for n in range(1, 5):  # the second Flatten is out: one at a time
        s, effects = core.on_bar(s, at(f"09:3{n}"))
        assert sends(effects) == []
    s, _ = core.on_fill(s, fill(-1, 20000.0, at("09:35").ts + timedelta(seconds=1)))
    assert (s.position, s.exiting) == (core.FLAT, None)


def test_a_flatten_that_fills_by_itself_sends_no_second_one():
    """An ordinary Flatten of a stopless position filled in parts: closing fills never end it."""
    s = started(sim(), at("09:30"))
    t = at("09:31").ts
    s, effects = core.on_fill(s, fill(2, 20000.0, t + timedelta(seconds=5)))
    [first] = sends(effects)
    s, effects = core.on_fill(s, fill(-1, 20000.0, t + timedelta(seconds=6)))
    assert (s.position.qty, s.exiting, sends(effects)) == (1, first.client_id, [])
    s, effects = core.on_bar(s, at("09:31"))
    assert sends(effects) == []
    s, effects = core.on_fill(s, fill(-1, 20000.0, t + timedelta(seconds=70)))
    assert (s.position, s.exiting, sends(effects)) == (core.FLAT, None, [])
    assert sends(core.on_bar(s, at("09:32"))[1]) == []


def test_a_manual_add_while_the_strategys_flatten_is_out_is_flattened_at_once():
    """The strategy's Flatten of a bracketed long is out; a manual +1 fills first. The add ends
    that Flatten, so the hub stops resending it if its post timed out (review of #236,
    non-blocking 3). The add has no stop at NinjaTrader (#237), so the 2 lots get their own
    Flatten at once, and the lot left after the strategy's Flatten fills stays under it (before:
    the bracket's stop was kept for 2 lots and nothing more went out)."""
    s = entered(sim())
    s, effects = decide_now(s, [Flatten()])
    [mine] = sends(effects)
    assert s.exiting == mine.client_id
    t = at("09:31").ts + timedelta(seconds=20)
    s, effects = core.on_fill(s, fill(1, 20000.0, t))
    cid = core.client_id("Sim101", t, "f2", Flatten())
    assert (s.position.qty, s.position.stop, s.exiting) == (2, None, cid)
    assert sends(effects) == [Send(Flatten(), cid)]
    s, effects = core.on_fill(s, fill(-1, 20000.0, t + timedelta(seconds=10)))  # the strategy's
    assert (s.position.qty, s.exiting, sends(effects)) == (1, cid, [])
    s, effects = core.on_bar(s, at("09:32"))
    assert sends(effects) == []  # one at a time


def test_more_of_the_entry_while_a_flatten_is_out_gets_its_own_flatten():
    """The same with the entry's own later fill, which keeps its stop: the Flatten it ends was
    sent for fewer lots, and nothing else sends another (a strategy's Flatten is not sent again
    by any bar), so the position gets its own at once."""
    b = at("09:30")
    s, _ = decide_now(started(sim(), b), [replace(LONG, qty=2)])
    entry_id = s.working.client_id
    t = b.ts + timedelta(minutes=1, seconds=1)
    s, _ = core.on_fill(s, fill(1, 20000.25, t, cid=entry_id))
    s, effects = decide_now(s, [Flatten()])
    [mine] = sends(effects)
    s, effects = core.on_fill(s, fill(1, 20000.25, t + timedelta(seconds=1), cid=entry_id))
    assert s.position == core.Position(2, 20000.25, 19990.0, 20020.0)
    cid = core.client_id("Sim101", t + timedelta(seconds=1), "f2", Flatten())
    assert sends(effects) == [Send(Flatten(), cid)] and s.exiting == cid
    why = f"position 2 filled while Flatten {mine.client_id} was out"
    assert reasons(effects) == [why]
    assert [e.kind for e in effects if isinstance(e, core.Log)][-2:] == ["alert", "action"]
    assert effects[-3].payload == {"message": f"flattening: {why}"}


def test_late_bar_takes_no_entries_but_exits():
    s = started(sim(), at("09:30"))
    closed = at("09:31").ts  # the 09:30 bar's close
    _, effects = core.decide(s, [LONG], closed + timedelta(seconds=90), 90)
    assert [e.action for e in sends(effects)] == [LONG]
    _, effects = core.decide(s, [LONG], closed + timedelta(seconds=91), 90)
    assert kinds(effects) == ["bar", "rejection"]
    reason = effects[1].payload["reason"]
    assert reason == "bar closed 91s ago (over 90s): no entry at its close"
    s = core.on_bar(entered(sim()), at("09:31"))[0]
    _, effects = core.decide(s, [Flatten()], s.last_seen + timedelta(seconds=91), 90)
    assert [e.action for e in sends(effects)] == [Flatten()]


def test_position_open_at_the_day_roll_is_still_a_breach():
    s = entered(sim(), b=at("16:30"))
    s, _ = core.on_bar(s, at("09:30", D2))
    assert (s.halt_kind, s.halted) == (
        "breach",
        "breach flat_by: position 1 still open at day close",
    )


def test_fill_in_the_next_trading_day_rolls_the_day_first():
    s = entered(sim())
    s, _ = core.on_fill(s, fill(-1, 20010.0, at("09:40").ts))
    evening = at("18:01").ts  # 18:01 ET on D1 is in trading day D2
    s, effects = core.on_fill(s, fill(1, 20000.0, evening))
    assert s.acct.day == D2 and s.acct.closed_days == ((D1, pytest.approx(9.75 * 20)),)
    assert s.acct.day_start_balance == pytest.approx(9.75 * 20)
    assert core.account_status(s)["session"]["fills"] == [evening.isoformat()]


def test_late_fill_from_a_closed_day_books_into_today():
    s, _ = core.on_bar(replace(started(sim(), at("16:00")), nt=NT), at("18:00"))  # trading day D2
    s, _ = core.on_fill(s, fill(1, 20000.0, at("16:20").ts))  # stamped in D1, arrives late
    assert s.acct.day == D2 and s.position.qty == 1
    s, effects = core.on_bar(s, at("18:01"))
    assert s.halted is None and kinds(effects) == []  # D1 is not closed again (flat_by breach)


@pytest.mark.parametrize(
    ("price", "paused"), [(20001.0, False), (20001.25, True), (19998.75, True)]
)
def test_entry_fill_drift_past_4_ticks_pauses_entries(price, paused):
    s, _ = decide_now(started(sim(), at("09:30")), [LONG])  # checked at the 20000.0 close
    s, effects = core.on_fill(
        s, fill(1, price, at("09:31").ts + timedelta(seconds=1), cid=s.working.client_id)
    )
    assert kinds(effects) == (["fill", "halt"] if paused else ["fill"])  # restored on restart
    assert (s.drift is not None) is paused
    if paused:
        st = core.account_status(s)
        assert (st["halted"], st["halt_kind"]) == (True, "drift")
        s, _ = core.on_fill(s, fill(-1, 20000.0, at("09:32").ts))
        s, _ = core.on_bar(s, at("09:33"))
        assert decide_now(s, [LONG])[1][1].payload["reason"].startswith("paused: entry fill")


# #220, owner decision: while NinjaTrader reports a balance at or below 0, no new entries;
# exits, stops and the runner's own Flattens still go out


def nt_account(balance):
    """An add-on "account" stream message for Sim101 (absolute $), no P&L today."""
    return {
        "type": "account",
        "id": "Sim101",
        "name": "Sim101",
        "is_sim": True,
        "balance": balance,
        "realized_pnl_today": 0.0,
        "unrealized_pnl": 0.0,
    }


def test_a_zero_balance_pauses_entries_until_one_above_0():
    s = started(sim(), at("09:30"))
    s, _ = core.on_account(s, nt_account(50_000.0), at("09:30").ts)
    s, _ = core.on_account(s, nt_account(0.0), at("09:30").ts)
    s, _ = core.on_bar(s, at("09:31"))
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["reason"] == f"paused: {core.NO_BALANCE}"
    st = core.account_status(s)
    assert (st["halted"], st["halt_kind"], st["halt_reason"]) == (True, "figures", core.NO_BALANCE)
    s, _ = core.on_account(s, nt_account(50_000.0), at("09:31").ts)
    s, _ = core.on_bar(s, at("09:32"))
    assert core.account_status(s)["halted"] is False
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "action", "send"]


def test_a_zero_balance_never_holds_back_an_exit():
    s = entered(sim())
    s, _ = core.on_account(s, nt_account(0.0), at("09:31").ts)
    s, _ = core.on_bar(s, at("09:32"))
    _, effects = decide_now(s, [Flatten()])
    assert [type(e.action) for e in sends(effects)] == [Flatten]
    late = at("16:39")  # flat-by: the runner's own Flatten
    s = entered(sim(), b=at("16:30"))
    s, _ = core.on_account(s, nt_account(0.0), late.ts)
    s, effects = core.on_bar(s, late)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", late.ts, "x", Flatten()))]


@pytest.mark.parametrize("before", [50_000.0, 0.0], ids=["figures", "zero"])
def test_a_reconnect_pauses_entries_until_a_balance_above_0(before):
    """#243: after a lost link the reconcile alone lets no Bracket out. NinjaTrader's figures
    went with the link (and a zero before the drop is no balance either), so entries wait for
    its next account message with a balance above 0; exits still go out."""
    s = started(sim(), at("09:30"))
    s, _ = core.on_account(s, nt_account(50_000.0), at("09:30").ts)
    s, _ = core.on_account(s, nt_account(before), at("09:30").ts)
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    s = reconciled(s)
    s, _ = core.on_bar(s, at("09:31"))
    assert s.unreconciled is None
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["reason"] == f"paused: {core.FIGURES_LOST}"
    s, _ = core.on_account(s, nt_account(0.0), at("09:31").ts)  # still no balance
    s, _ = core.on_bar(s, at("09:32"))
    _, effects = decide_now(s, [LONG])
    assert effects[1].payload["reason"] == f"paused: {core.NO_BALANCE}"
    s, _ = core.on_account(s, nt_account(50_000.0), at("09:32").ts)
    s, _ = core.on_bar(s, at("09:33"))
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "action", "send"]


def test_a_lost_link_never_holds_back_an_exit():
    s = entered(sim())
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    s, _ = core.on_bar(s, at("09:32"))
    _, effects = decide_now(s, [Flatten()])
    assert [type(e.action) for e in sends(effects)] == [Flatten]
