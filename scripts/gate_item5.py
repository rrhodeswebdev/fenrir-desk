"""LSE data gate item 5: LSE bars vs NinjaTrader sim fills. Throwaway; not imported by the app.

Reads a sim account's journal (`<private_dir>/fenrir.db`, read-only): its `fill` rows (the
add-on's fills) and its `bar` rows (the LSE 1-minute bars the runner journalled). Each fill is
compared with the bar of the minute holding the fill time: how many ticks the fill price lies
outside that bar's [low, high]. No LSE call: the bars are the ones the runner traded on.
Bars and fills meet on the full contract code (`MNQ 12-26` -> `NQZ26`), so a month mismatch is
unmatched, not compared. Any unmatched fill FAILs unless `--allow-unmatched N` accepts up to N;
each one accepted must be explained in docs/data-gate.md Item 5.

Run (repo root, where `instruments.yaml` is):
     uv run python scripts/gate_item5.py Sim101 --day 2026-10-15 2026-10-16
     uv run python scripts/gate_item5.py Sim101 --day 2026-10-15 --db /path/to/fenrir.db
     uv run python scripts/gate_item5.py Sim101 --day 2026-10-15 --allow-unmatched 1
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


def seconds_in(ts: datetime) -> float:
    """Seconds past the minute: an entry fill lands about :02-:05 after the bar it was sent on,
    so a fill near :00 on a FAIL line points at a clock offset between NinjaTrader and LSE."""
    return (ts - minute(ts)).total_seconds()


_MONTH_CODES = {"03": "H", "06": "M", "09": "U", "12": "Z"}


def contract_of(instrument: str, specs: Mapping[str, ContractSpec]) -> tuple[str, ContractSpec]:
    """NinjaTrader's name to our bar's contract code and the fill's own spec: `MNQ 12-26` ->
    (`NQZ26`, MNQ), the inverse of `fenrir.bridge.names.nt_instrument`."""
    symbol, _, expiry = instrument.partition(" ")
    month, _, year = expiry.partition("-")
    if symbol not in specs or month not in _MONTH_CODES or len(year) != 2 or not year.isdigit():
        raise ValueError(
            f"fill on instrument {instrument!r}: not a quarterly contract of instruments.yaml"
        )
    spec = specs[symbol]
    return f"{spec.series}{_MONTH_CODES[month]}{year}", spec


def bars_by_minute(bars: Iterable[Mapping]) -> dict[tuple[str, datetime], tuple[float, float]]:
    """Journalled `bar` payloads keyed by (contract code, bar-open minute in UTC) -> (low, high).
    A bar covers [ts, ts + 1 min)."""
    return {(b["contract"], minute(parse_ts(b["ts"]))): (b["low"], b["high"]) for b in bars}


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
    unmatched: tuple[Mapping, ...]  # fills with no bar of their contract for their minute
    skipped: int = 0  # qty-0 fills: no trade, never compared nor counted toward MIN_FILLS


def compare_fills(
    fills: Iterable[Mapping],
    bars: Mapping[tuple[str, datetime], tuple[float, float]],
    specs: Mapping[str, ContractSpec],
) -> Result:
    """`specs` by symbol; a fill's instrument is NinjaTrader's name (`MNQ 12-26`)."""
    seen, skipped, compared, unmatched = 0, 0, [], []
    for f in fills:
        seen += 1
        if f["qty"] == 0:  # the runner journals and ignores it: no trade to compare
            skipped += 1
            continue
        contract, spec = contract_of(f["instrument"], specs)
        bar = bars.get((contract, minute(parse_ts(f["ts"]))))
        if bar is None:
            unmatched.append(f)
            continue
        low, high = bar
        compared.append(Compared(f, low, high, ticks_outside(f["price"], low, high, spec.tick)))
    return Result(seen, tuple(compared), tuple(unmatched), skipped)


def beyond(result: Result) -> list[Compared]:
    return [c for c in result.compared if c.ticks > TOLERANCE_TICKS + EPS]


def passed(result: Result, allow_unmatched: int = 0) -> bool:
    """An unmatched fill is one the gate could not check, so any FAILs unless the owner accepts
    up to `allow_unmatched` of them, each explained in docs/data-gate.md Item 5."""
    return (
        len(result.compared) >= MIN_FILLS
        and not beyond(result)
        and len(result.unmatched) <= allow_unmatched
    )


def _at(f: Mapping) -> str:
    return f"{f['ts']} (+{seconds_in(parse_ts(f['ts'])):.3f}s)"


def report(result: Result, allow_unmatched: int = 0) -> list[str]:
    worst = f"{max(c.ticks for c in result.compared):.2f}" if result.compared else "n/a"
    lines = [
        f"fills seen={result.seen} compared={len(result.compared)}"
        f" unmatched={len(result.unmatched)} skipped={result.skipped} worst={worst} ticks"
        f" (tolerance {TOLERANCE_TICKS} tick)"
    ]
    lines += [
        f"   no bar: {_at(f)} {f['instrument']} qty={f['qty']} price={f['price']}"
        for f in result.unmatched
    ]
    lines += [
        f"   beyond: {_at(c.fill)} {c.fill['instrument']} qty={c.fill['qty']}"
        f" price={c.fill['price']} bar low={c.low} high={c.high} -> {c.ticks:.2f} ticks"
        for c in beyond(result)
    ]
    if result.unmatched:
        lines.append(
            f"   {len(result.unmatched)} unmatched, {allow_unmatched} allowed:"
            " explain each in docs/data-gate.md Item 5"
        )
    if len(result.compared) < MIN_FILLS:
        lines.append(f"   {len(result.compared)} fills compared, need {MIN_FILLS}")
    lines.append("PASS" if passed(result, allow_unmatched) else "FAIL")
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
    p.add_argument(
        "--db", type=Path, help="default: <private_dir>/fenrir.db as this shell's settings read it"
    )
    p.add_argument(
        "--allow-unmatched",
        type=int,
        default=0,
        metavar="N",
        help="accept up to N fills with no bar (default 0); explain each in data-gate Item 5",
    )
    p.add_argument("--instruments", type=Path, default=Path("instruments.yaml"))
    args = p.parse_args()
    db = args.db or load_settings().private_dir / "fenrir.db"
    fills, bars = load(db, args.account, args.day)
    specs = {s.symbol: s for s in load_instruments(args.instruments).specs}
    print(f"journal {db} account {args.account} days {', '.join(map(str, args.day))}")
    try:
        result = compare_fills(fills, bars_by_minute(bars), specs)
    except ValueError as e:  # e.g. a fill on an instrument instruments.yaml does not list
        raise SystemExit(f"FAIL: {e}") from None
    for line in report(result, args.allow_unmatched):
        print(line)


if __name__ == "__main__":
    main()
