"""Consistency: no single day may exceed max_day_pct of total profit."""

from collections.abc import Sequence


def consistent(day_pnls: Sequence[float], max_day_pct: float) -> bool:
    total = sum(day_pnls)
    return total > 0 and max(day_pnls) <= total * max_day_pct / 100
