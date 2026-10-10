from dataclasses import replace

from fenrir.rules.consistency import consistent
from fenrir.rules.contracts import contract_limit, max_minis
from fenrir.rules.microscalp import microscalp
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import TradeClosed


def test_consistency_broken_by_one_big_day():
    assert not consistent((2500.0, 300.0, 200.0), 40)  # 2500 > 40% of 3000


def test_consistency_holds_when_spread_out():
    assert consistent((1000.0, 900.0, 800.0, 700.0), 40)  # 1000 <= 40% of 3400


def test_losing_days_reduce_the_total():
    assert not consistent((1200.0, -200.0, 800.0, 700.0), 40)  # 1200 > 40% of 2500


def test_no_profit_is_not_consistent():
    assert not consistent((), 40)
    assert not consistent((-100.0, 50.0), 40)


def trade(et, pnl, same_bar):
    return TradeClosed(opened=et("10:00"), closed=et("10:00"), pnl=pnl, same_bar=same_bar)


def test_same_bar_winner_is_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, 50.0, True)).possible_microscalp_pnl == 50.0


def test_same_bar_loser_not_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, -50.0, True)).possible_microscalp_pnl == 0.0


def test_multi_bar_winner_not_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, 50.0, False)).possible_microscalp_pnl == 0.0


def test_contract_limit_with_micros(base):
    rules = AccountRules.model_validate(base)  # max_minis 4
    limit = rules.contracts.max_minis
    assert contract_limit(limit, 3.5, 5 * 0.1) is None
    reason = contract_limit(limit, 3.5, 6 * 0.1)
    assert "contract limit" in reason and "4" in reason


def test_micro_float_sums_at_limit(base):
    base["contracts"]["max_minis"] = 3
    limit = AccountRules.model_validate(base).contracts.max_minis
    assert 0.1 + 29 * 0.1 > 3  # 3.0000000000000004 in floats
    assert contract_limit(limit, 0.1, 29 * 0.1) is None  # 30 micros = 3 minis: allowed
    assert contract_limit(limit, 0.1, 30 * 0.1) is not None


def scaled(base):
    base["funded"]["scaling"] = [
        {"from_profit": 0, "max_minis": 2},
        {"from_profit": 1000, "max_minis": 3},
        {"from_profit": 2000, "max_minis": 4},
    ]
    return AccountRules.model_validate(base)


def test_funded_scaling_uses_session_start_balance(base, make_state):
    rules = scaled(base)
    funded = make_state(phase="funded", balance=2500.0)
    assert max_minis(rules, replace(funded, day_start_balance=0.0)) == 2
    assert max_minis(rules, replace(funded, day_start_balance=1000.0)) == 3  # live balance ignored
    assert max_minis(rules, replace(funded, day_start_balance=2000.0)) == 4
    assert max_minis(rules, replace(funded, day_start_balance=-500.0)) == 2


def test_evaluation_ignores_scaling(base, make_state):
    rules = scaled(base)
    assert max_minis(rules, make_state(day_start_balance=0.0)) == 4


def test_no_scaling_uses_flat_limit(base, make_state):
    rules = AccountRules.model_validate(base)
    assert max_minis(rules, make_state(phase="funded", day_start_balance=5000.0)) == 4
