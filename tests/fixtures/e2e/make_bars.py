"""Writes the tiny synthetic NQ bar set the Playwright flows run on, into a data dir.

Run from the repo root: uv run python tests/fixtures/e2e/make_bars.py <data_dir>
(web/e2e/global-setup.ts does, into its temp data dir; never point it at the repo data/).
Two 5-day windows a year apart, so a 12-month hold-out leaves days on both sides.
Price climbs one tick a minute, so e2e_bracket.py's daily trade (2 NQ, +$783 net) always hits
its target and lucid-pro-25k's $1,250 is reached on an attempt's 2nd day: each period then has
passed attempts and equity samples (a pass on a period's last day does not count).
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
MINUTES = 23 * 60  # 18:00 ET the evening before to 17:00 ET


def day_bars(day: date, contract: str, price: float) -> list[Bar]:
    start = datetime.combine(day - timedelta(days=1), time(18), NY).astimezone(UTC)
    return [
        Bar(start + timedelta(minutes=m), p, p + 0.25, p, p + 0.25, 100, contract)
        for m in range(MINUTES)
        for p in [price + 0.25 * m]
    ]


def main(out: Path) -> None:
    instruments = load_instruments()
    by_contract: dict[str, list[Bar]] = defaultdict(list)
    for i, day in enumerate(DAYS):
        contract = front_month(instruments, "NQ", day)
        by_contract[contract].extend(day_bars(day, contract, 18000.0 + 0.25 * MINUTES * i))
    for contract, bars in by_contract.items():
        write_bars(out, "NQ", contract, bars)
        print(f"{contract}: {len(bars)} bars")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
