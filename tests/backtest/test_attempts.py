from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, bar

from fenrir.backtest.attempts import day_starts, months_before, periods


@pytest.mark.parametrize(
    ("d", "n", "expected"),
    [
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2026, 1, 15), 1, date(2025, 12, 15)),
        (date(2026, 10, 10), 12, date(2025, 10, 10)),
        (date(2024, 3, 31), 1, date(2024, 2, 29)),
        (date(2026, 5, 20), 0, date(2026, 5, 20)),
    ],
)
def test_months_before(d, n, expected):
    assert months_before(d, n) == expected


def test_day_starts_use_trading_days():
    bars = (
        bar("09:30", 1, 1, 1, 1, D1),
        bar("09:31", 1, 1, 1, 1, D1),
        bar("18:00", 1, 1, 1, 1, D1),  # 18:00 ET belongs to the next trading day
        bar("09:30", 1, 1, 1, 1, D2),
    )
    assert day_starts(bars) == ((D1, 0), (D2, 2))


def weekday_bars(start: date, end: date):
    days = (start + timedelta(n) for n in range((end - start).days + 1))
    return tuple(bar("09:30", 1, 1, 1, 1, d) for d in days if d.weekday() < 5)


def test_periods_split_on_months():
    bars = weekday_bars(date(2025, 1, 1), date(2025, 6, 30))
    tuning, holdout = periods(bars, 2)
    split = next(i for i, b in enumerate(bars) if b.ts.date() >= date(2025, 5, 1))
    assert (tuning.start, tuning.end) == (date(2025, 1, 1), date(2025, 4, 30))
    assert (holdout.start, holdout.end) == (date(2025, 5, 1), date(2025, 6, 30))
    assert len(tuning.ranges) + len(holdout.ranges) == len(bars)
    assert all(end == split for _, end in tuning.ranges)  # tuning never sees hold-out bars
    assert all(end == len(bars) for _, end in holdout.ranges)
    assert tuning.ranges[0] == (0, split)


def test_zero_months_means_no_holdout():
    bars = weekday_bars(date(2025, 1, 1), date(2025, 1, 31))
    tuning, holdout = periods(bars, 0)
    assert holdout is None
    assert len(tuning.ranges) == len(bars)
    assert all(end == len(bars) for _, end in tuning.ranges)


def test_holdout_covering_all_data_is_an_error():
    with pytest.raises(ValueError, match="hold-out of 12 months covers all data"):
        periods(weekday_bars(date(2025, 1, 1), date(2025, 3, 31)), 12)


def test_no_bars_is_an_error():
    with pytest.raises(ValueError, match="no bars"):
        periods((), 12)
