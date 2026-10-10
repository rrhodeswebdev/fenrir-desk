from datetime import date

import pytest

from fenrir.rules.account import apply, build_rules, initial_state, passed, payout, view, with_dll
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import Breach, DayClosed, Fill, Mark

D1, D2, D3, D4 = (date(2026, 10, d) for d in (13, 14, 15, 16))


def trade_day(et, day, pnl):
    return (
        Fill(ts=et("10:00", day), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0),
        Fill(
            ts=et("10:30", day), qty=-1, price=20000.0 + pnl / 20, commission=0.0, realized_pnl=pnl
        ),
        DayClosed(day=day, ts=et("17:00", day)),
    )


def run(rules, phase, events):
    fns = build_rules(rules, phase)
    state = initial_state(rules, phase, D1)
    for event in events:
        state = apply(fns, state, event)
        if isinstance(state, Breach):
            break
    return state


def test_initial_state(base):
    s = initial_state(AccountRules.model_validate(base), "evaluation", D1)
    assert (s.phase, s.balance, s.floor, s.position) == ("evaluation", 0.0, -2000.0, 0)


def test_direct_has_no_evaluation(base):
    base["evaluation"] = None
    rules = AccountRules.model_validate(base)
    with pytest.raises(ValueError, match="no evaluation"):
        initial_state(rules, "evaluation", D1)
    assert initial_state(rules, "funded", D1).phase == "funded"


def test_build_rules_includes_only_set_kinds(base):
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 4
    base["evaluation"]["daily_loss_limit"] = 1000
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 5
    base["prohibited"]["microscalp"] = None
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 4


def test_passes_when_target_met(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0))
    assert s.balance == 3100.0 and passed(s, rules)


def test_pass_needs_flat(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 3100.0))
    s = apply(
        build_rules(rules, "evaluation"),
        s,
        Fill(ts=et("10:00", D2), qty=1, price=1.0, commission=0.0, realized_pnl=0.0),
    )
    assert not passed(s, rules)


def test_min_trading_days(base, et):
    base["evaluation"]["min_trading_days"] = 3
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0))
    assert not passed(s, rules)
    s = run(
        rules,
        "evaluation",
        trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0) + trade_day(et, D3, 100.0),
    )
    assert passed(s, rules)


def test_consistency_blocks_pass_until_profit_spreads(base, et):
    base["evaluation"]["consistency"] = {"max_day_pct": 40}
    rules = AccountRules.model_validate(base)
    two = trade_day(et, D1, 2500.0) + trade_day(et, D2, 600.0)
    assert not passed(run(rules, "evaluation", two), rules)  # 2500 > 40% of 3100
    four = two + trade_day(et, D3, 1500.0) + trade_day(et, D4, 1700.0)
    assert passed(run(rules, "evaluation", four), rules)  # 2500 <= 40% of 6300


def test_first_payout_amount(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "funded", trade_day(et, D1, 2000.0))
    assert payout(s, rules) == 900.0  # 2000 x 50% x 90%


def test_payout_waits_for_min_days(base, et):
    base["funded"]["payout"]["min_days"] = 5
    rules = AccountRules.model_validate(base)
    assert payout(run(rules, "funded", trade_day(et, D1, 2000.0)), rules) is None


def test_no_payout_on_loss_or_in_evaluation(base, et):
    rules = AccountRules.model_validate(base)
    assert payout(run(rules, "funded", trade_day(et, D1, -500.0)), rules) is None
    assert payout(run(rules, "evaluation", trade_day(et, D1, 2000.0)), rules) is None


def test_view(base, make_state):
    base["evaluation"]["daily_loss_limit"] = 1000
    rules = AccountRules.model_validate(base)
    v = view(
        make_state(balance=1000.0, open_pnl=-200.0, day_start_balance=600.0, floor=-1000.0), rules
    )
    assert (v.drawdown_remaining, v.today_pnl, v.daily_loss_remaining, v.target_remaining) == (
        1800.0,
        200.0,
        1200.0,
        2000.0,
    )
    assert view(make_state(phase="funded"), rules).target_remaining is None


def test_drawdown_breach_fails_even_with_dll_on(base, et):
    base["evaluation"]["daily_loss_limit"] = 500
    rules = AccountRules.model_validate(base)
    events = (
        Fill(ts=et("10:00"), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0),
        Mark(ts=et("10:00"), worst_open_pnl=-2500.0, best_open_pnl=0.0, close_open_pnl=-2400.0),
    )
    out = run(rules, "evaluation", events)
    assert (
        isinstance(out, Breach) and out.rule == "max_drawdown"
    )  # -2500 <= -2000 floor: still a hard failure


def test_dll_halts_the_day_without_failing(base, et):
    base["evaluation"]["daily_loss_limit"] = 500
    rules = AccountRules.model_validate(base)
    s = run(
        rules,
        "evaluation",
        (
            Fill(ts=et("10:00"), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0),
            Mark(
                ts=et("10:00"), worst_open_pnl=-600.0, best_open_pnl=0.0, close_open_pnl=-500.0
            ),  # -600 <= -500
            Fill(
                ts=et("10:01"), qty=-1, price=19975.0, commission=0.0, realized_pnl=-500.0
            ),  # the engine flattens
        ),
    )
    assert not isinstance(s, Breach) and s.halted_today and s.balance == -500.0
    s = apply(build_rules(rules, "evaluation"), s, DayClosed(day=D1, ts=et("17:00", D1)))
    assert not s.halted_today and s.closed_days == (
        (D1, -500.0),
    )  # next trading day trades normally


def test_dll_applies_only_when_on(base):
    base["evaluation"]["daily_loss_limit"] = 1000
    base["funded"]["daily_loss_limit"] = 1000
    rules = AccountRules.model_validate(base)
    assert with_dll(rules, True) == rules
    off = with_dll(rules, False)
    assert (off.evaluation.daily_loss_limit, off.funded.daily_loss_limit) == (None, None)
    assert len(build_rules(off, "evaluation")) == 4


def test_dll_on_without_amount_is_an_error(base):
    base["evaluation"]["daily_loss_limit"] = 1000  # funded stays null
    with pytest.raises(ValueError, match="no daily_loss_limit amount for funded"):
        with_dll(AccountRules.model_validate(base), True)


FLEX = {"min_days": 5, "min_profit": 500, "min_day_profit": 150, "max_amount": 2000}


def flex_rules(base):
    base["funded"]["payout"].update(FLEX)
    return AccountRules.model_validate(base)


def funded_with(make_state, balance, days):
    return make_state(
        phase="funded",
        balance=balance,
        closed_days=tuple((date(2026, 10, 13 + i), d) for i, d in enumerate(days)),
    )


def test_payout_counts_only_qualifying_days(base, make_state):
    rules = flex_rules(base)
    assert payout(funded_with(make_state, 8000.0, (200, 200, 100, 100, 100)), rules) is None


def test_payout_request_is_capped(base, make_state):
    rules = flex_rules(base)
    assert (
        payout(funded_with(make_state, 8000.0, (200,) * 5), rules) == 1800.0
    )  # min(4000, 2000) x 90%


def test_payout_below_minimum_request(base, make_state):
    rules = flex_rules(base)
    assert payout(funded_with(make_state, 900.0, (200,) * 5), rules) is None  # 450 < 500


def test_passes_at_exact_target_despite_float_drift(base, make_state):
    rules = AccountRules.model_validate(base)
    balance = 3000.03 - 0.01 - 0.01 - 0.01  # three commissions: 2999.9999999999995
    assert passed(make_state(balance=balance), rules)
    assert not passed(make_state(balance=2999.99), rules)


def test_payout_counts_day_at_exact_min_day_profit(base, make_state):
    rules = flex_rules(base)
    assert payout(funded_with(make_state, 8000.0, (150,) * 5), rules) == 1800.0
    assert payout(funded_with(make_state, 8000.0, (150,) * 4 + (149.99,)), rules) is None


def test_payout_at_exact_minimum_request(base, make_state):
    rules = flex_rules(base)
    assert payout(funded_with(make_state, 1000.0, (200,) * 5), rules) == 450.0  # 500 x 90%


def test_dll_on_without_evaluation_amount_is_an_error(base):
    base["funded"]["daily_loss_limit"] = 1000  # evaluation stays null
    with pytest.raises(ValueError, match="no daily_loss_limit amount for evaluation$"):
        with_dll(AccountRules.model_validate(base), True)


def test_dll_on_direct_account_checks_funded_only(base):
    base["evaluation"] = None
    with pytest.raises(ValueError, match="no daily_loss_limit amount for funded$"):
        with_dll(AccountRules.model_validate(base), True)
    base["funded"]["daily_loss_limit"] = 1000
    rules = AccountRules.model_validate(base)
    assert with_dll(rules, True) == rules
