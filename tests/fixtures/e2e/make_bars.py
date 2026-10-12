"""Writes the tiny synthetic NQ bar set the Playwright flows run on, into a data dir.

Run from the repo root: uv run python tests/fixtures/e2e/make_bars.py <data_dir>
(web/e2e/global-setup.ts does, into its temp data dir; never point it at the repo data/).
Two 5-day windows a year apart, so a 12-month hold-out leaves days on both sides.
Price climbs one tick a minute, so strategies/orb.py wins every day and a $1,250 target is
reached on an attempt's 4th day: each period then has a passed attempt and an equity sample.
"""

import sys
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from fenrir.core.bars import Bar
from fenrir.core.instruments import front_month, load_instruments
from fenrir.core.sessions import NY
from fenrir.data.store import write_bars

DAYS = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)] + [date(2025, 6, d) for d in (2, 3, 4, 5, 6)]


def day_bars(day: date, contract: str, price: float) -> tuple[list[Bar], float]:
    start = datetime.combine(day - timedelta(days=1), time(18), NY).astimezone(UTC)
    bars = []
    for m in range(23 * 60):  # 18:00 ET the evening before to 17:00 ET
        up = price + 0.25
        bars.append(Bar(start + timedelta(minutes=m), price, up, price, up, 100, contract))
        price = up
    return bars, price


def main(out: Path) -> None:
    instruments = load_instruments()
    price = 18000.0
    by_contract: dict[str, list[Bar]] = defaultdict(list)
    for day in DAYS:
        contract = front_month(instruments, "NQ", day)
        bars, price = day_bars(day, contract, price)
        by_contract[contract].extend(bars)
    for contract, bars in by_contract.items():
        write_bars(out, "NQ", contract, bars)
        print(f"{contract}: {len(bars)} bars")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
