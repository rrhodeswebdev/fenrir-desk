from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fenrir.core.bars import Bar
from fenrir.data.live import Tick, close_due, empty_builder, feed

T0 = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def feed_all(state, ticks):
    emitted = ()
    for tick in ticks:
        state, bars = feed(state, tick)
        emitted += bars
    return state, emitted


def test_first_tick_opens_a_bar_but_emits_nothing():
    state, bars = feed(empty_builder(), Tick(at(5), 100.0, 2, "NQZ26"))
    assert bars == ()
    _, closed = close_due(state, at(60))
    assert closed == (Bar(T0, 100.0, 100.0, 100.0, 100.0, 2, "NQZ26"),)


def test_ticks_in_one_minute_build_ohlc_and_sum_volume():
    ticks = [
        Tick(at(1), 100.0, 1, "NQZ26"),
        Tick(at(10), 102.5, 3, "NQZ26"),
        Tick(at(20), 99.25, 2, "NQZ26"),
        Tick(at(59.9), 101.0, 4, "NQZ26"),
    ]
    state, bars = feed_all(empty_builder(), ticks)
    assert bars == ()
    _, closed = close_due(state, at(60))
    assert closed == (Bar(T0, 100.0, 102.5, 99.25, 101.0, 10, "NQZ26"),)


def test_tick_in_a_new_minute_closes_the_previous_bar():
    state, bars = feed_all(
        empty_builder(),
        [Tick(at(1), 100.0, 1, "NQZ26"), Tick(at(30), 101.0, 1, "NQZ26")],
    )
    state, bars = feed(state, Tick(at(61), 105.0, 2, "NQZ26"))
    assert bars == (Bar(T0, 100.0, 101.0, 100.0, 101.0, 2, "NQZ26"),)
    _, closed = close_due(state, at(120))
    assert closed == (Bar(at(60), 105.0, 105.0, 105.0, 105.0, 2, "NQZ26"),)


def test_close_due_waits_until_the_minute_has_ended():
    state, _ = feed(empty_builder(), Tick(at(5), 100.0, 1, "NQZ26"))
    state, early = close_due(state, at(59.999))
    assert early == ()
    state, closed = close_due(state, at(60))
    assert closed == (Bar(T0, 100.0, 100.0, 100.0, 100.0, 1, "NQZ26"),)
    assert close_due(state, at(300))[1] == ()


def test_no_ticks_no_bars():
    assert close_due(empty_builder(), at(600))[1] == ()


def test_contracts_build_separate_bars():
    state, bars = feed_all(
        empty_builder(),
        [Tick(at(1), 100.0, 1, "NQZ26"), Tick(at(2), 5000.0, 3, "ESZ26")],
    )
    assert bars == ()
    state, bars = feed(state, Tick(at(61), 101.0, 1, "NQZ26"))
    assert bars == (Bar(T0, 100.0, 100.0, 100.0, 100.0, 1, "NQZ26"),)
    _, closed = close_due(state, at(60))
    assert closed == (Bar(T0, 5000.0, 5000.0, 5000.0, 5000.0, 3, "ESZ26"),)


def test_late_ticks_for_a_closed_minute_are_dropped():
    state, _ = feed_all(
        empty_builder(),
        [Tick(at(1), 100.0, 1, "NQZ26"), Tick(at(61), 101.0, 1, "NQZ26")],
    )
    state, bars = feed(state, Tick(at(30), 90.0, 9, "NQZ26"))  # older than the open bar
    assert bars == ()
    state, closed = close_due(state, at(120))
    assert closed == (Bar(at(60), 101.0, 101.0, 101.0, 101.0, 1, "NQZ26"),)
    state, bars = feed(state, Tick(at(90), 90.0, 9, "NQZ26"))  # its minute was closed by close_due
    assert bars == ()
    assert close_due(state, at(180))[1] == ()


def test_tick_time_is_floored_to_the_utc_minute():
    ny = T0.astimezone(ZoneInfo("America/New_York")) + timedelta(seconds=42)
    state, _ = feed(empty_builder(), Tick(ny, 100.0, 1, "NQZ26"))
    assert close_due(state, at(60))[1][0].ts == T0


def test_naive_tick_time_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        feed(empty_builder(), Tick(datetime(2026, 10, 12, 14, 30), 100.0, 1, "NQZ26"))


def test_naive_close_due_time_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        close_due(empty_builder(), datetime(2026, 10, 12, 14, 31))
