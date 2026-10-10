from fenrir.rules.daily_loss import daily_loss_limit
from fenrir.rules.state import Mark


def mark(et, worst):
    return Mark(ts=et("11:00"), worst_open_pnl=worst, best_open_pnl=0.0, close_open_pnl=worst)


def test_halt_at_exact_limit(make_state, et):
    rule = daily_loss_limit(1000.0)
    # a day halt, not a Breach
    assert rule(make_state(balance=-600.0), mark(et, -400.0)).halted_today


def test_no_halt_above_limit(make_state, et):
    assert not daily_loss_limit(1000.0)(make_state(balance=-600.0), mark(et, -399.0)).halted_today


def test_yesterdays_loss_not_counted(make_state, et):
    s = make_state(balance=-900.0, day_start_balance=-900.0)
    assert not daily_loss_limit(1000.0)(s, mark(et, -100.0)).halted_today


def test_halt_at_limit_despite_float_drift(make_state, et):
    balance = -600.0 - 0.06 - 0.06 - 0.06  # three commissions
    assert daily_loss_limit(1000.0)(make_state(balance=balance), mark(et, -399.82)).halted_today
