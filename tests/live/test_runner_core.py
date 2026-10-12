from dataclasses import replace
from datetime import timedelta

import pytest
from backtest_helpers import D1, NQ, bar, make_rules, rules_data, scripted
from live_helpers import make_setup

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket, Cancel, Flatten
from fenrir.live import core
from fenrir.live.core import Start
from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import DayClosed

BAR = bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0)
LONG = Bracket("long", 1, 19990.0, 20020.0)  # stop-out: (10 + 1 tick) x $20 = $205


def run(setup, b=BAR, state=None):
    """Start (unless given a state), feed one bar; return the state before decide."""
    s = state or core.start(setup, b.ts)[0]
    return core.on_bar(s, b)[0]


def decide(setup, actions, b=BAR):
    return core.decide(run(setup, b), actions)


def kinds(effects):
    return [e.kind for e in effects]


def scaled_rules() -> AccountRules:
    d = rules_data()
    d["funded"]["scaling"] = [
        {"from_profit": 0, "max_minis": 2},
        {"from_profit": 1000, "max_minis": 4},
    ]
    return AccountRules.model_validate(d)


def test_dry_bar_logs_bar_and_unsent_bracket():
    s, effects = decide(make_setup(make_rules()), [LONG])
    assert kinds(effects) == ["bar", "action"]
    a = effects[1].payload
    assert (a["type"], a["side"], a["qty"]) == ("Bracket", "long", 1)
    assert (a["sent"], a["mode"]) == (False, "dry")
    assert effects[0].payload["actions"] == 1
    assert s.position.qty == 0  # dry run never fills


def test_ctx_window_is_capped_and_ends_with_the_bar():
    s, _ = core.start(make_setup(make_rules()), BAR.ts)
    for i in range(core.WINDOW + 5):
        last = replace(BAR, ts=BAR.ts + timedelta(minutes=i))
        s, _ = core.on_bar(s, last)
    c = core.ctx(s)
    assert len(c.bars) == core.WINDOW and c.bars[-1] == last and c.ts == last.ts
    assert c.account.drawdown_remaining == 2000.0


def test_over_contract_limit_is_rejected():
    _, effects = decide(make_setup(make_rules()), [replace(LONG, qty=5)])
    assert kinds(effects) == ["bar", "rejection"]
    assert "max_minis 4" in effects[1].payload["reason"]
    assert effects[1].payload["action"]["qty"] == 5


def test_funded_scaling_limits_size_and_status():
    low = Start(phase_start=50_000.0, peak=50_500.0, balance=50_500.0)  # +500: tier 2
    setup = make_setup(scaled_rules(), mode="sim", phase="funded", start=low)
    s = run(setup)
    assert core.account_status(s)["limits"]["max_minis"] == 2
    _, effects = core.decide(s, [replace(LONG, qty=3)])
    assert "max_minis 2" in effects[1].payload["reason"]

    high = Start(phase_start=50_000.0, peak=51_500.0, balance=51_500.0)  # +1500: tier 4
    s = run(replace(setup, start=high))
    assert core.account_status(s)["limits"]["max_minis"] == 4
    assert kinds(core.decide(s, [replace(LONG, qty=3)])[1]) == ["bar", "action"]


def test_stop_out_past_drawdown_is_rejected_from_ninjatrader_start():
    # peak +1000 → floor -1000; balance +500 → $1,500 of room, so a $1,505 stop-out breaches
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)
    wide = Bracket("long", 1, 19925.0, 20020.0)
    _, effects = decide(make_setup(make_rules(), mode="sim", start=start), [wide])
    assert kinds(effects) == ["bar", "rejection"]  # journalled, nothing sent (SC5)
    assert "drawdown floor -1000.00" in effects[1].payload["reason"]
    # the same bracket on a fresh account has $2,000 of room
    assert kinds(decide(make_setup(make_rules()), [wide])[1]) == ["bar", "action"]


def test_stop_out_past_daily_loss_limit_is_rejected():
    rules = make_rules(daily_loss=500.0)
    _, effects = decide(make_setup(rules), [Bracket("long", 1, 19975.0, 20020.0)])  # $505
    assert kinds(effects) == ["bar", "rejection"]
    assert "daily loss limit" in effects[1].payload["reason"]
    assert kinds(decide(make_setup(rules), [Bracket("long", 1, 19976.0, 20020.0)])[1]) == [
        "bar",
        "action",
    ]


def test_no_entries_at_or_after_flat_time():
    late = bar("16:39", 20000.0, 20005.0, 19995.0, 20000.0)  # closes 16:40 ET
    _, effects = decide(make_setup(make_rules()), [LONG, Flatten()], late)
    assert kinds(effects) == ["bar", "rejection", "action"]  # exits still allowed
    assert "16:40" in effects[1].payload["reason"]
    early = bar("16:38", 20000.0, 20005.0, 19995.0, 20000.0)
    assert kinds(decide(make_setup(make_rules()), [LONG], early)[1]) == ["bar", "action"]


def test_one_bracket_per_bar_and_flatten_cancel_pass():
    _, effects = decide(make_setup(make_rules()), [LONG, LONG, Cancel("x"), LONG])
    assert kinds(effects) == ["bar", "action", "rejection", "action", "action"]
    assert effects[2].payload["reason"] == "a bracket was already accepted this bar"


def test_halted_account_rejects_brackets():
    s, effects = core.halt(run(make_setup(make_rules())), "test")
    assert kinds(effects) == ["halt"]
    _, effects = core.decide(s, [LONG])
    assert effects[1].payload["reason"] == "halted: test"


def test_strategy_error_halts_account():
    s, effects = core.step_failed(run(make_setup(make_rules())), "RuntimeError('bad index')")
    assert kinds(effects) == ["bar", "halt"]
    assert "bad index" in s.halted and s.halt_day is None
    assert kinds(core.decide(s, [LONG])[1]) == ["bar", "rejection"]


def test_changed_strategy_file_halts_at_start():
    s, effects = core.start(make_setup(make_rules(), strategy_hash="h2"), BAR.ts)
    assert kinds(effects) == ["halt"] and "changed since arming" in s.halted
    assert kinds(core.decide(run(None, state=s), [LONG])[1]) == ["bar", "rejection"]


def test_sim_without_start_balances_halts_and_dry_ignores_them():
    s, effects = core.start(make_setup(make_rules(), mode="sim"), BAR.ts)
    assert kinds(effects) == ["halt"] and "DEC-4a" in s.halted
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)
    s, effects = core.start(make_setup(make_rules(), start=start), BAR.ts)
    assert effects == () and (s.acct.balance, s.acct.floor) == (0.0, -2000.0)


@pytest.mark.parametrize("locks_at", [None, 2100.0])
def test_start_floor_matches_the_drawdown_rule(locks_at):
    d = rules_data()
    d["evaluation"]["max_drawdown"]["locks_at"] = locks_at
    rules = AccountRules.model_validate(d)
    start = Start(phase_start=50_000.0, peak=53_000.0, balance=50_200.0)
    acct = core.start_account(rules, "evaluation", D1, start)
    assert (acct.balance, acct.day_start_balance, acct.peak) == (200.0, 200.0, 3000.0)
    # the floor the backtest's rule reaches after closing a day at the same peak
    fresh = core.start_account(rules, "evaluation", D1, None)
    trailed = max_drawdown(rules.evaluation.max_drawdown)(
        replace(fresh, balance=3000.0), DayClosed(D1, BAR.ts)
    )
    assert acct.floor == trailed.floor == (100.0 if locks_at else 1000.0)


@pytest.mark.parametrize(
    ("daily_loss", "level"),
    [(500.0, -400.0), (None, -1600.0)],  # 0 − 0.8 × 500; −2000 + 0.2 × 2000 when the DLL is off
)
def test_safety_margin_halts_for_the_day(daily_loss, level):
    s = run(make_setup(make_rules(daily_loss=daily_loss)))
    assert core.safety_level(s) == level
    near = replace(s, acct=replace(s.acct, balance=level + 1))
    assert core.on_bar(near, BAR)[1] == ()
    s, effects = core.on_bar(replace(s, acct=replace(s.acct, balance=level)), BAR)
    assert kinds(effects) == ["halt"]
    assert (s.halt_kind, s.halt_day) == ("safety", s.acct.day)
    assert s.halted.startswith("safety margin")
    s, effects = core.on_bar(s, BAR)
    assert effects == ()  # one halt, not one per bar
    assert core.decide(s, [LONG])[1][1].kind == "rejection"


def test_safety_drawdown_leg_uses_the_session_start_room():
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)  # room 1500
    s = run(make_setup(make_rules(), mode="sim", start=start))
    assert core.safety_level(s) == pytest.approx(-1000.0 + 0.2 * 1500)


def test_account_status_shape():
    st = core.account_status(run(make_setup(make_rules())))
    assert set(st) == {
        "account", "mode", "phase", "strategy", "rules_account", "contract", "halted",
        "halt_kind", "halt_reason", "balance", "today_pnl", "limits", "position", "session",
    }  # fmt: skip
    assert st["limits"] == {
        "drawdown_remaining": 2000.0,
        "drawdown_max": 2000.0,
        "daily_loss_remaining": None,
        "daily_loss_max": None,
        "max_minis": 4,
        "held_minis": 0.0,
    }
    assert st["session"] == {
        "start": 0.0,
        "floor": -2000.0,
        "target": 3000.0,
        "safety": -1600.0,
        "equity": [[BAR.ts.isoformat(), 0.0]],
        "fills": [],
    }
    assert (st["strategy"], st["halted"], st["halt_kind"], st["position"]["qty"]) == (
        "orb",
        False,
        None,
        0,
    )


@pytest.mark.parametrize(
    ("hhmm", "bracket", "daily_loss"),
    [
        ("09:30", LONG, None),
        ("09:30", replace(LONG, qty=5), None),  # contract limit
        ("09:30", Bracket("long", 1, 19900.0, 20020.0), None),  # drawdown
        ("09:30", Bracket("long", 1, 19901.0, 20020.0), None),
        ("09:30", Bracket("long", 1, 19975.0, 20020.0), 500.0),  # daily loss limit
        ("09:30", Bracket("short", 1, 20010.0, 19980.0), 500.0),
        ("09:30", Bracket("long", 1, 20010.0, 20020.0), None),  # stop on the wrong side
        ("16:39", LONG, None),  # flat time
    ],
)
def test_accepts_exactly_what_the_backtest_accepts(hhmm, bracket, daily_loss):
    rules = make_rules(daily_loss=daily_loss)
    b = bar(hhmm, 20000.0, 20005.0, 19995.0, 20000.0)
    following = replace(b, ts=b.ts + timedelta(minutes=1))
    result = run_attempt((b, following), 0, 2, scripted({hhmm: [bracket]}), rules, NQ)
    _, effects = decide(make_setup(rules), [bracket], b)
    assert (kinds(effects)[1] == "action") == bool(result.fills)
