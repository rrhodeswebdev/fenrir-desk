from datetime import date

from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.schema import Drawdown
from fenrir.rules.state import Breach, DayClosed, Mark

D = date(2026, 10, 13)


def rule(trailing="end_of_day", locks_at=None):
    return max_drawdown(Drawdown(amount=2000, trailing=trailing, locks_at=locks_at))


def mark(et, worst, best, close, hhmm="10:00"):
    return Mark(ts=et(hhmm), worst_open_pnl=worst, best_open_pnl=best, close_open_pnl=close)


def test_eod_floor_does_not_move_intraday(make_state, et):
    s = rule()(make_state(), mark(et, 0.0, 1500.0, 1500.0))
    assert (s.peak, s.floor) == (0.0, -2000.0)


def test_eod_trails_at_day_close(make_state, et):
    s = rule()(make_state(balance=1500.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (1500.0, -500.0)


def test_floor_never_moves_down(make_state, et):
    state = make_state(balance=1000.0, peak=1500.0, floor=-500.0)
    s = rule()(state, DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (1500.0, -500.0)


def test_breach_when_worst_equity_touches_floor(make_state, et):
    out = rule()(make_state(balance=-1500.0), mark(et, -500.0, 0.0, -100.0))
    assert isinstance(out, Breach) and out.rule == "max_drawdown"


def test_no_breach_one_dollar_above_floor(make_state, et):
    out = rule()(make_state(balance=-1500.0), mark(et, -499.0, 0.0, -100.0))
    assert not isinstance(out, Breach)


def test_trailing_locks(make_state, et):
    r = rule(locks_at=2100.0)
    s = r(make_state(balance=1000.0), DayClosed(day=D, ts=et("17:00")))
    assert s.floor == -1000.0  # still trailing below the lock
    s = r(make_state(balance=3000.0, peak=1000.0, floor=-1000.0), DayClosed(day=D, ts=et("17:00")))
    assert s.floor == 100.0  # locked at locks_at - amount
    s = r(make_state(balance=5000.0, peak=3000.0, floor=100.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (5000.0, 100.0)


def test_intraday_trails_on_best_then_checks_worst(make_state, et):
    # Up 1500 then down 600 inside one bar: floor trails to -500 first, so -600 breaches.
    out = rule("intraday")(make_state(), mark(et, -600.0, 1500.0, 0.0))
    assert isinstance(out, Breach)


def test_intraday_trails_without_breach(make_state, et):
    s = rule("intraday")(make_state(), mark(et, -100.0, 800.0, 500.0))
    assert (s.peak, s.floor) == (800.0, -1200.0)


def test_intraday_does_not_trail_at_day_close(make_state, et):
    s = rule("intraday")(make_state(balance=1000.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (0.0, -2000.0)


def test_day_close_breach(make_state, et):
    out = rule()(make_state(balance=-2000.0), DayClosed(day=D, ts=et("17:00")))
    assert isinstance(out, Breach)
