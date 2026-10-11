from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from fenrir.core.instruments import Instruments


class NoBars(RuntimeError):
    pass


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


def todo(windows: tuple[Window, ...], manifest: Mapping[str, Any]) -> tuple[Window, ...]:
    def done(w: Window) -> bool:
        entry = manifest.get("series", {}).get(w.series, {}).get(w.contract)
        return (
            bool(entry) and entry["complete"] and date.fromisoformat(entry["first_day"]) <= w.first
        )

    return tuple(w for w in windows if not done(w))


def pull(
    data_dir: Path,
    instruments: Instruments,
    series: tuple[str, ...],
    since: date,
    today: date,
    fetch: Callable[[Window], pl.DataFrame],
    now: Callable[[], str],
) -> tuple[Window, ...]:
    from fenrir.data.store import read_manifest, with_contract, write_contract, write_manifest

    manifest = read_manifest(data_dir)
    windows = todo(
        tuple(w for s in series for w in pull_windows(instruments, s, since, today)),
        manifest,
    )
    for w in windows:
        frame = fetch(w)
        # an empty window recorded as complete would never be re-pulled
        if frame.is_empty():
            raise NoBars(f"LSE returned no bars for {w.contract} {w.first}..{w.last}")
        rows = write_contract(data_dir, w.series, w.contract, frame)
        # manifest after every contract, so an interrupted pull keeps what finished
        manifest = with_contract(manifest, w, rows, today, now())
        write_manifest(data_dir, manifest)
    return windows
