"""Where attempts start and which bars each may see. Pure."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from fenrir.core.bars import Bar
from fenrir.core.sessions import trading_day


@dataclass(frozen=True, slots=True)
class Period:
    start: date  # first trading day
    end: date  # last trading day
    ranges: tuple[tuple[int, int], ...]  # (first bar, end bar) per attempt, one per trading day


def months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def day_starts(bars: tuple[Bar, ...]) -> tuple[tuple[date, int], ...]:
    out, last = [], None
    for i, b in enumerate(bars):
        day = trading_day(b.ts)
        if day != last:
            out.append((day, i))
            last = day
    return tuple(out)


def periods(bars: tuple[Bar, ...], holdout_months: int) -> tuple[Period, Period | None]:
    starts = day_starts(bars)
    if not starts:
        raise ValueError("no bars")
    first, last = starts[0][0], starts[-1][0]
    cutoff = months_before(last, holdout_months) if holdout_months > 0 else last
    tuning = [s for s in starts if s[0] <= cutoff]
    holdout = [s for s in starts if s[0] > cutoff]
    if not tuning:
        raise ValueError(f"hold-out of {holdout_months} months covers all data ({first} to {last})")
    split = holdout[0][1] if holdout else len(bars)
    return _period(tuning, split), _period(holdout, len(bars)) if holdout else None


def _period(starts: list[tuple[date, int]], end: int) -> Period:
    return Period(starts[0][0], starts[-1][0], tuple((i, end) for _, i in starts))
