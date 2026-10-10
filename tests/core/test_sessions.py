from datetime import UTC, date, datetime

import pytest

from fenrir.core.sessions import at_or_after, trading_day


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_trading_day_daytime_is_same_date():
    # 2026-10-08 14:30 UTC = 10:30 EDT
    assert trading_day(utc(2026, 10, 8, 14, 30)) == date(2026, 10, 8)


def test_trading_day_evening_belongs_to_next_day():
    # 2026-10-08 22:00 UTC = 18:00 EDT -> session for Friday 2026-10-09
    assert trading_day(utc(2026, 10, 8, 22, 0)) == date(2026, 10, 9)


def test_trading_day_just_before_open_is_same_day():
    # 21:59 UTC = 17:59 EDT
    assert trading_day(utc(2026, 10, 8, 21, 59)) == date(2026, 10, 8)


def test_trading_day_sunday_open_is_monday():
    # Sunday 2026-10-11 22:00 UTC = 18:00 EDT
    assert trading_day(utc(2026, 10, 11, 22, 0)) == date(2026, 10, 12)


def test_trading_day_across_dst_start():
    # DST starts Sunday 2026-03-08; 22:30 UTC = 18:30 EDT
    assert trading_day(utc(2026, 3, 8, 22, 30)) == date(2026, 3, 9)


def test_trading_day_across_dst_end():
    # DST ends Sunday 2026-11-01; 23:00 UTC = 18:00 EST, 22:30 UTC = 17:30 EST
    assert trading_day(utc(2026, 11, 1, 23, 0)) == date(2026, 11, 2)
    assert trading_day(utc(2026, 11, 1, 22, 30)) == date(2026, 11, 1)


def test_trading_day_rejects_naive():
    with pytest.raises(ValueError, match="naive"):
        trading_day(datetime(2026, 10, 8, 14, 30))


def test_at_or_after_same_session():
    assert at_or_after(utc(2026, 10, 8, 20, 45), "16:45")  # 16:45 EDT
    assert not at_or_after(utc(2026, 10, 8, 20, 44), "16:45")  # 16:44 EDT
    assert at_or_after(utc(2026, 10, 8, 21, 30), "16:45")  # 17:30 EDT


def test_at_or_after_evening_belongs_to_next_session():
    # 19:00 EDT is the start of the next trading day, long before its 16:45 flat-by
    assert not at_or_after(utc(2026, 10, 8, 23, 0), "16:45")


def test_at_or_after_other_timezone():
    # 20:45 UTC is 15:45 in Chicago (CDT)
    assert not at_or_after(utc(2026, 10, 8, 20, 45), "15:50", tz="America/Chicago")
    assert at_or_after(utc(2026, 10, 8, 20, 50), "15:50", tz="America/Chicago")


def test_at_or_after_other_timezone_evening_belongs_to_next_session():
    # 22:30 UTC = 18:30 EDT = 17:30 CDT: the next ET trading day, long before its 15:50 CT flat-by
    assert not at_or_after(utc(2026, 10, 8, 22, 30), "15:50", tz="America/Chicago")
