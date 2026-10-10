from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fenrir.core.instruments import Instruments


@dataclass(frozen=True, slots=True)
class Window:
    series: str
    contract: str
    first: date  # first trading day pulled, inclusive
    last: date  # last trading day pulled, inclusive


def pull_windows(
    instruments: Instruments, series: str, since: date, today: date
) -> tuple[Window, ...]:
    rolls = [r for r in instruments.rolls if r.series == series]
    ends = [r.start - timedelta(days=1) for r in rolls[1:]] + [today]
    windows = (
        Window(series, r.contract, max(r.start, since), min(end, today))
        for r, end in zip(rolls, ends, strict=True)
    )
    return tuple(w for w in windows if w.first <= w.last)


def todo(
    windows: tuple[Window, ...], manifest: Mapping[str, Any], today: date
) -> tuple[Window, ...]:
    def done(w: Window) -> bool:
        entry = manifest.get("series", {}).get(w.series, {}).get(w.contract)
        return (
            bool(entry) and entry["complete"] and date.fromisoformat(entry["first_day"]) <= w.first
        )

    return tuple(w for w in windows if not done(w))
