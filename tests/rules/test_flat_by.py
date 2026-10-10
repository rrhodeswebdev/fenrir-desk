from datetime import date

from fenrir.rules.flat_by import flat_by
from fenrir.rules.state import Breach, DayClosed, Fill, Mark

RULE = flat_by("16:45", "America/New_York")


def mark(et, hhmm):
    return Mark(ts=et(hhmm), worst_open_pnl=0.0, best_open_pnl=0.0, close_open_pnl=0.0)


def test_position_before_flat_by_ok(make_state, et):
    assert not isinstance(RULE(make_state(position=1), mark(et, "16:44")), Breach)


def test_position_at_flat_by_breaches(make_state, et):
    out = RULE(make_state(position=1), mark(et, "16:45"))
    assert isinstance(out, Breach) and out.rule == "flat_by"


def test_flat_after_flat_by_ok(make_state, et):
    assert not isinstance(RULE(make_state(), mark(et, "16:50")), Breach)


def test_evening_session_is_next_trading_day(make_state, et):
    assert not isinstance(RULE(make_state(position=1), mark(et, "18:30")), Breach)


def test_opening_fill_after_flat_by_breaches(make_state, et):
    fill = Fill(ts=et("16:46"), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0)
    assert isinstance(RULE(make_state(position=1), fill), Breach)  # rules see post-fill position


def test_day_close_with_position_breaches(make_state, et):
    event = DayClosed(day=date(2026, 10, 13), ts=et("17:00"))
    assert isinstance(RULE(make_state(position=-2), event), Breach)
