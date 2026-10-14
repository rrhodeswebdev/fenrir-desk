"""LSE data gate item 5: LSE bars vs NinjaTrader sim fills. Throwaway; not imported by the app.

Reads a sim account's journal (`<private_dir>/fenrir.db`, read-only): its `fill` rows (the
add-on's fills) and its `bar` rows (the LSE 1-minute bars the runner journalled). Each fill is
compared with the bar of the minute holding the fill time: how many ticks the fill price lies
outside that bar's [low, high]. No LSE call: the bars are the ones the runner traded on.

Run (repo root, where `instruments.yaml` is):
     uv run python scripts/gate_item5.py Sim101 --day 2026-10-15 2026-10-16
     uv run python scripts/gate_item5.py Sim101 --day 2026-10-15 --db /path/to/fenrir.db
"""

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fenrir.core.instruments import ContractSpec, load_instruments
from fenrir.live import journal
from fenrir.settings import load_settings

MIN_FILLS = 10  # fewer compared is FAIL: a key mismatch must never pass with nothing compared
# item 4's tolerance: LSE's live bars matched history within 1 tick. A sim market fill prints at
# the ask or bid while LSE's `price` may be the bid (docs/data-gate.md Item 3): 1 tick covers that
TOLERANCE_TICKS = 1
EPS = 1e-9  # LSE prices carry float noise (7827.250000000001)


# --- pure helpers ---


def parse_ts(text: str) -> datetime:
    """A journal timestamp as aware UTC: the add-on's `2026-10-12T13:31:01.123Z` and a bar
    row's `2026-10-12 13:31:00+00:00` (json `default=str` of a datetime) alike."""
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp {text!r}")
    return ts.astimezone(UTC)


def minute(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(second=0, microsecond=0)


def series_of(contract: str) -> str:
    """`NQZ26` -> `NQ` (our contract codes: series + month letter + two-digit year)."""
    return contract[:-3]


def bars_by_minute(bars: Iterable[Mapping]) -> dict[tuple[str, datetime], tuple[float, float]]:
    """Journalled `bar` payloads keyed by (series, bar-open minute in UTC) -> (low, high).
    A bar covers [ts, ts + 1 min)."""
    return {
        (series_of(b["contract"]), minute(parse_ts(b["ts"]))): (b["low"], b["high"]) for b in bars
    }


def ticks_outside(price: float, low: float, high: float, tick: float) -> float:
    """How far `price` lies outside [low, high], in ticks; 0 inside."""
    return max(low - price, price - high, 0.0) / tick


@dataclass(frozen=True, slots=True)
class Compared:
    fill: Mapping
    low: float
    high: float
    ticks: float


@dataclass(frozen=True, slots=True)
class Result:
    seen: int
    compared: tuple[Compared, ...]
    unmatched: tuple[Mapping, ...]  # fills with no bar for their minute


def compare_fills(
    fills: Iterable[Mapping],
    bars: Mapping[tuple[str, datetime], tuple[float, float]],
    specs: Mapping[str, ContractSpec],
) -> Result:
    """`specs` by symbol; a fill's instrument is NinjaTrader's name (`MNQ 12-26`)."""
    seen, compared, unmatched = 0, [], []
    for f in fills:
        seen += 1
        spec = specs[f["instrument"].split()[0]]
        bar = bars.get((spec.series, minute(parse_ts(f["ts"]))))
        if bar is None:
            unmatched.append(f)
            continue
        low, high = bar
        compared.append(Compared(f, low, high, ticks_outside(f["price"], low, high, spec.tick)))
    return Result(seen, tuple(compared), tuple(unmatched))


def beyond(result: Result) -> list[Compared]:
    return [c for c in result.compared if c.ticks > TOLERANCE_TICKS + EPS]


def passed(result: Result) -> bool:
    return len(result.compared) >= MIN_FILLS and not beyond(result)


def report(result: Result) -> list[str]:
    worst = f"{max(c.ticks for c in result.compared):.2f}" if result.compared else "n/a"
    lines = [
        f"fills seen={result.seen} compared={len(result.compared)}"
        f" unmatched={len(result.unmatched)} worst={worst} ticks"
        f" (tolerance {TOLERANCE_TICKS} tick)"
    ]
    lines += [
        f"   no bar: {f['ts']} {f['instrument']} qty={f['qty']} price={f['price']}"
        for f in result.unmatched
    ]
    lines += [
        f"   beyond: {c.fill['ts']} {c.fill['instrument']} qty={c.fill['qty']}"
        f" price={c.fill['price']} bar low={c.low} high={c.high} -> {c.ticks:.2f} ticks"
        for c in beyond(result)
    ]
    if len(result.compared) < MIN_FILLS:
        lines.append(f"   {len(result.compared)} fills compared, need {MIN_FILLS}")
    lines.append("PASS" if passed(result) else "FAIL")
    return lines


# --- shell ---


def load(db: Path, account: str, days: list[date]) -> tuple[list[dict], list[dict]]:
    conn = journal.open_ro(db)  # never creates the db; WAL readers never block the hub
    try:
        fills = [r.payload for d in days for r in journal.rows(conn, account, d, "fill")]
        bars = [r.payload for d in days for r in journal.rows(conn, account, d, "bar")]
    finally:
        conn.close()
    return fills, bars


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("account", help="the NinjaTrader sim account, as armed (e.g. Sim101)")
    p.add_argument("--day", nargs="+", required=True, type=date.fromisoformat, help="trading days")
    p.add_argument("--db", type=Path, help="default: <private_dir>/fenrir.db")
    p.add_argument("--instruments", type=Path, default=Path("instruments.yaml"))
    args = p.parse_args()
    db = args.db or load_settings().private_dir / "fenrir.db"
    fills, bars = load(db, args.account, args.day)
    specs = {s.symbol: s for s in load_instruments(args.instruments).specs}
    print(f"journal {db} account {args.account} days {', '.join(map(str, args.day))}")
    for line in report(compare_fills(fills, bars_by_minute(bars), specs)):
        print(line)


if __name__ == "__main__":
    main()
