from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from backtest_helpers import D1, D2, NQ, bar, make_rules, scripted
from live_helpers import make_setup

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


def started(setup, b):
    return core.on_bar(core.start(setup, b.ts)[0], b)[0]


def fill(qty, price, ts, commission=0.0, cid="other"):
    """An add-on "fill" stream message (validated by fenrir.bridge.client)."""
    stamp = ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "type": "fill",
        "account": "Sim101",
        "client_id": cid,
        "order_id": "o",
        "instrument": "NQ 12-25",
        "qty": qty,
        "price": price,
        "commission": commission,
        "ts": stamp,
    }


def kinds(effects):
    return [e.kind if isinstance(e, core.Log) else "send" for e in effects]


def sends(effects):
    return [e for e in effects if isinstance(e, Send)]


def entered(setup, price=20000.25, b=None):
    """Sim account with LONG sent on `b` (09:30) and its entry filled 1 s into the next bar."""
    b = b or at("09:30")
    s, _ = core.decide(started(setup, b), [LONG])
    entry = fill(1, price, b.ts + timedelta(minutes=1, seconds=1), cid=s.working.client_id)
    return core.on_fill(s, entry)[0]


def test_client_id_is_deterministic_and_at_most_24_chars():
    ts = at("09:30").ts
    a = core.client_id("Lucid-Account-0001-with-a-long-name", ts, "0")
    assert a == core.client_id("Lucid-Account-0001-with-a-long-name", ts, "0")
    assert len(a) <= 24 and a.startswith("2510061330-")
    others = {
        core.client_id("Lucid-Account-0002-with-a-long-name", ts, "0"),
        core.client_id("Lucid-Account-0001-with-a-long-name", ts + timedelta(minutes=1), "0"),
        core.client_id("Lucid-Account-0001-with-a-long-name", ts, "1"),
        core.client_id("Lucid-Account-0001-with-a-long-name", ts, "x"),
    }
    assert a not in others and len(others) == 4
    assert all(len(c) <= 24 for c in others)


def test_dry_mode_never_sends():
    s = started(make_setup(make_rules()), at("09:30"))
    s, effects = core.decide(s, [Flatten(), Cancel("o1"), LONG])
    assert sends(effects) == [] and s.working is None
    assert [e.payload["sent"] for e in effects[1:]] == [False, False, False]


def test_sim_sends_accepted_actions_with_their_client_ids():
    b = at("09:30")
    s, effects = core.decide(started(sim(), b), [Flatten(), Cancel("o1"), LONG])
    # a Cancel is never sent: step cannot know NinjaTrader's order ids (backtest: same-bar only)
    assert kinds(effects) == ["bar", "action", "send", "action", "action", "send"]
    ids = [core.client_id("Sim101", b.ts, str(i)) for i in range(3)]
    assert sends(effects) == [Send(Flatten(), ids[0]), Send(LONG, ids[2])]
    logs = [effects[i].payload for i in (1, 3, 4)]
    assert [(p["sent"], p.get("client_id")) for p in logs] == [
        (True, ids[0]),
        (False, None),
        (True, ids[2]),
    ]
    assert s.working == core.Working(ids[2], LONG, b.close)


def test_rejected_and_replaced_brackets_are_not_sent():
    s, effects = core.decide(started(sim(), at("09:30")), [LONG, Flatten()])
    assert kinds(effects) == ["bar", "rejection", "action", "send"]
    assert sends(effects)[0].action == Flatten() and s.working is None


def test_working_entry_blocks_a_second_bracket_until_it_fills():
    b0, b1 = at("09:30"), at("09:31")
    s, _ = core.decide(started(sim(), b0), [LONG])
    s, _ = core.on_bar(s, b1)  # the entry fill is late
    s2, effects = core.decide(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"] and s2.working == s.working
    assert "still working" in effects[1].payload["reason"]
    s, effects = core.on_fill(
        s, fill(1, 20000.25, b1.ts + timedelta(seconds=30), cid=s.working.client_id)
    )
    assert kinds(effects) == ["fill"] and s.working is None
    assert s.position == core.Position(1, 20000.25, 19990.0, 20020.0)
    assert core.decide(s, [LONG])[1][1].payload["reason"] == "already in a position"


def test_a_fill_that_is_not_the_entry_keeps_it_working():
    s, _ = core.decide(started(sim(), at("09:30")), [LONG])
    w = s.working
    s, _ = core.on_fill(s, fill(1, 20000.0, at("09:31").ts))  # e.g. a manual order
    assert s.working == w and s.position == core.Position(1, 20000.0, None, None)
    s, _ = core.on_fill(s, fill(-1, 20000.0, at("09:31").ts + timedelta(seconds=5)))
    assert s.working == w  # flat again, and still no second bracket until it resolves
    assert "still working" in core.decide(s, [LONG])[1][1].payload["reason"]


def test_failed_send_alerts_and_frees_the_entry():
    s, effects = core.decide(started(sim(), at("09:30")), [LONG])
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


def test_partial_entry_fills_average_the_entry():
    s = entered(sim())
    s, _ = core.on_fill(s, fill(1, 20001.25, at("09:31").ts + timedelta(seconds=2)))
    assert s.position == core.Position(2, 20000.75, 19990.0, 20020.0)


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
    ],
)
def test_live_fills_book_like_the_backtest(trailing, daily_loss, exit_bar):
    rules = make_rules(commission=1.0, trailing=trailing, daily_loss=daily_loss)
    bars = (at("09:30"), at("09:31"), exit_bar)
    expected, fills = backtest_account(bars, {"09:30": [LONG]}, rules)
    assert len(fills) == 2
    s = started(replace(sim(), rules=rules), bars[0])
    s, _ = core.decide(s, [LONG])
    cid = s.working.client_id
    for b, f in zip(bars[1:], fills, strict=True):
        s, _ = core.on_fill(s, fill(f.qty, f.price, f.ts + timedelta(seconds=1), cid=cid))
        s, _ = core.on_bar(s, b)
    assert s.acct == replace(expected, day=s.acct.day)


def test_daily_loss_limit_hit_flattens_once():
    s = entered(sim(daily_loss=500.0))
    deep = bar("09:31", 20000.0, 20000.0, 19990.0, 19992.0)  # worst at the stop: −$205
    s, effects = core.on_bar(s, deep)
    assert sends(effects) == []  # −$205 is above −$500
    s = replace(s, acct=replace(s.acct, balance=-300.0))  # an earlier loss today
    b = at("09:32", c=19992.0, lo=19990.0)
    s, effects = core.on_bar(s, b)
    assert s.acct.halted_today and (s.halt_kind, s.halt_day) == ("safety", D1)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", b.ts, "x"))]
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
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", b.ts, "x"))]
    assert sends(core.on_bar(s, at("09:32"))[1]) == []


def test_flat_by_flattens_and_the_day_rolls_after_the_exit_fill():
    s = entered(sim(), b=at("16:30"))
    late = at("16:39")  # closes at 16:40
    s, effects = core.on_bar(s, at("16:38"))
    assert sends(effects) == []
    s, effects = core.on_bar(s, late)
    assert sends(effects) == [Send(Flatten(), core.client_id("Sim101", late.ts, "x"))]
    assert sends(core.on_bar(s, at("16:40"))[1]) == []
    s, _ = core.on_fill(s, fill(-1, 20004.75, at("16:40").ts + timedelta(seconds=1)))
    s, effects = core.on_bar(s, at("18:00"))  # 18:00 ET opens trading day D2
    assert s.halted is None and "halt" not in kinds(effects)
    assert s.acct.closed_days == ((D1, pytest.approx(4.5 * 20)),)
    assert core.account_status(s)["session"]["fills"] == []


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
    s, _ = core.on_bar(started(sim(), at("16:00")), at("18:00"))  # trading day D2
    s, _ = core.on_fill(s, fill(1, 20000.0, at("16:20").ts))  # stamped in D1, arrives late
    assert s.acct.day == D2 and s.position.qty == 1
    s, effects = core.on_bar(s, at("18:01"))
    assert s.halted is None and kinds(effects) == []  # D1 is not closed again (flat_by breach)


@pytest.mark.parametrize(
    ("price", "paused"), [(20001.0, False), (20001.25, True), (19998.75, True)]
)
def test_entry_fill_drift_past_4_ticks_pauses_entries(price, paused):
    s, _ = core.decide(started(sim(), at("09:30")), [LONG])  # checked at the 20000.0 close
    s, effects = core.on_fill(
        s, fill(1, price, at("09:31").ts + timedelta(seconds=1), cid=s.working.client_id)
    )
    assert kinds(effects) == (["fill", "alert"] if paused else ["fill"])
    assert (s.drift is not None) is paused
    if paused:
        st = core.account_status(s)
        assert (st["halted"], st["halt_kind"]) == (True, "drift")
        s, _ = core.on_fill(s, fill(-1, 20000.0, at("09:32").ts))
        s, _ = core.on_bar(s, at("09:33"))
        assert core.decide(s, [LONG])[1][1].payload["reason"].startswith("paused: entry fill")
