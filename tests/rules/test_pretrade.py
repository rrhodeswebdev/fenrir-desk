from datetime import date

import pytest

from fenrir.core.actions import Bracket
from fenrir.core.instruments import ContractSpec
from fenrir.rules.ledger import book
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import DayClosed

NQ = ContractSpec(symbol="NQ", series="NQ", point_value=20.0, tick=0.25, minis=1.0)
MNQ = ContractSpec(symbol="MNQ", series="NQ", point_value=2.0, tick=0.25, minis=0.1)


def long(stop, target=20100.0, qty=1):
    return Bracket(side="long", qty=qty, stop=stop, target=target)


def run(state, base, bracket, spec=NQ, held=0.0):
    return check(state, AccountRules.model_validate(base), bracket, spec, 20000.0, held)


def test_accepts_affordable_bracket(base, make_state):
    assert run(make_state(), base, long(19980.0)) is None


def test_rejects_stop_out_through_floor(base, make_state):
    reason = run(make_state(), base, long(19900.0))
    assert "drawdown" in reason  # (100 + 0.25) x 20 = 2005 > 2000


def test_stop_out_exactly_at_floor_rejected(base, make_state):
    assert run(make_state(), base, long(19900.25)) is not None  # loss 2000


def test_rejects_stop_out_through_daily_loss(base, make_state):
    base["evaluation"]["daily_loss_limit"] = 500
    reason = run(make_state(), base, long(19975.0))
    assert "daily loss" in reason  # 25.25 x 20 = 505


def test_halted_day_refuses_entries_until_day_close(base, make_state, et):
    rules, halted = AccountRules.model_validate(base), make_state(halted_today=True)
    assert "daily loss limit" in check(halted, rules, long(19980.0), NQ, 20000.0, 0.0)
    next_day = book(halted, DayClosed(day=date(2026, 10, 13), ts=et("17:00")))
    assert check(next_day, rules, long(19980.0), NQ, 20000.0, 0.0) is None


def test_round_trip_commission_counts(base, make_state):
    s, b = make_state(balance=-7.0), long(19900.75)  # stop-out 1990 before commission
    assert run(s, base, b) is None
    base["costs"]["commission_per_side"]["NQ"] = 2.0
    assert "drawdown" in run(s, base, b)


def test_contract_limit_with_micros(base, make_state):
    assert run(make_state(), base, long(19990.0, qty=5), MNQ, 3.5) is None
    assert "contract limit" in run(make_state(), base, long(19990.0, qty=6), MNQ, 3.5)


def test_funded_contract_limit_follows_scaling_tier(base, make_state):
    base["funded"]["scaling"] = [
        {"from_profit": 0, "max_minis": 2},
        {"from_profit": 1000, "max_minis": 3},
        {"from_profit": 2000, "max_minis": 4},
    ]
    b = long(19990.0, qty=3)
    assert "contract limit" in run(make_state(phase="funded", day_start_balance=0.0), base, b)
    assert run(make_state(phase="funded", day_start_balance=1000.0), base, b) is None


@pytest.mark.parametrize(
    ("side", "stop", "target"), [("long", 20010.0, 20100.0), ("short", 19990.0, 19900.0)]
)
def test_wrong_side_stop_rejected(base, make_state, side, stop, target):
    b = Bracket(side=side, qty=1, stop=stop, target=target)
    assert "stop" in run(make_state(), base, b)


def test_wrong_side_target_rejected(base, make_state):
    b = Bracket(side="short", qty=1, stop=20020.0, target=20010.0)
    assert "target" in run(make_state(), base, b)


def test_zero_qty_rejected(base, make_state):
    assert "qty" in run(make_state(), base, long(19980.0, qty=0))


def short(stop, target=19900.0):
    return Bracket(side="short", qty=1, stop=stop, target=target)


def test_accepts_affordable_short(base, make_state):
    assert run(make_state(), base, short(20020.0)) is None


def test_short_stop_out_exactly_at_floor_rejected(base, make_state):
    assert "drawdown" in run(make_state(), base, short(20099.75))  # (99.75 + 0.25) x 20 = 2000


def test_stop_out_exactly_at_daily_loss_rejected(base, make_state):
    base["evaluation"]["daily_loss_limit"] = 500
    assert "daily loss" in run(make_state(), base, short(20024.75))  # (24.75 + 0.25) x 20 = 500
    assert run(make_state(), base, short(20024.5)) is None  # 495
