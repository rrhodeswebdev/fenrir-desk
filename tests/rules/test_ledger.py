from datetime import date

from fenrir.rules.ledger import book
from fenrir.rules.state import DayClosed, Fill, Mark


def test_opening_fill_charges_commission_and_tracks_position(make_state, et):
    s = book(
        make_state(), Fill(ts=et("10:00"), qty=2, price=20000.0, commission=1.5, realized_pnl=0.0)
    )
    assert (s.balance, s.position, s.traded_today) == (-1.5, 2, True)


def test_closing_fill_realizes_and_clears_open_pnl(make_state, et):
    s = make_state(position=2, open_pnl=300.0)
    s = book(s, Fill(ts=et("10:30"), qty=-2, price=20010.0, commission=1.5, realized_pnl=400.0))
    assert (s.balance, s.position, s.open_pnl) == (398.5, 0, 0.0)


def test_partial_close_keeps_open_pnl_until_next_mark(make_state, et):
    s = make_state(position=2, open_pnl=300.0)
    s = book(s, Fill(ts=et("10:30"), qty=-1, price=20010.0, commission=0.0, realized_pnl=200.0))
    assert (s.position, s.open_pnl) == (1, 300.0)


def test_mark_sets_open_pnl_and_trading_day(make_state, et):
    s = book(
        make_state(),
        Mark(ts=et("19:00"), worst_open_pnl=-50.0, best_open_pnl=200.0, close_open_pnl=125.0),
    )
    assert (s.open_pnl, s.day) == (
        125.0,
        date(2026, 10, 14),
    )  # 19:00 ET belongs to the next trading day


def test_day_closed_records_traded_day_and_resets(make_state, et):
    s = make_state(balance=500.0, day_start_balance=200.0, traded_today=True, halted_today=True)
    s = book(s, DayClosed(day=date(2026, 10, 13), ts=et("17:00")))
    assert s.closed_days == ((date(2026, 10, 13), 300.0),)
    assert (s.day_start_balance, s.traded_today, s.halted_today) == (500.0, False, False)


def test_day_closed_skips_untraded_day(make_state, et):
    s = book(
        make_state(balance=500.0, day_start_balance=500.0),
        DayClosed(day=date(2026, 10, 13), ts=et("17:00")),
    )
    assert s.closed_days == ()
