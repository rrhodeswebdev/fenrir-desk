"""LSE data gate (spec §3 items 1-4). Throwaway; not imported by the app.

Run: uv run --with lse-data python scripts/lse_gate.py history ES.F --start 2010-01-01
     uv run --with lse-data python scripts/lse_gate.py live NQ.F ES.F --minutes 10
     uv run --with lse-data python scripts/lse_gate.py compare data/gate/ticks-2026-10-12.jsonl
"""

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fenrir.core.sessions import trading_day
from fenrir.settings import load_settings

OUT = Path("data/gate")
PAGE = 5000  # vault max rows per candles() call

# --- LSE API (lse-data 0.14.0, see docs/data-gate.md "Client API") ---


def fetch_history(api_key: str, symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Rows as dicts: ts (aware UTC, bar OPEN), open, high, low, close, volume, contract=None."""
    from lse import LSE

    client = LSE(api_key=api_key)
    rows: list[dict] = []
    cursor = start
    while cursor < end:
        page = client.candles(
            symbol,
            "1m",
            start=cursor.strftime("%Y-%m-%dT%H:%M:%S"),  # naive strings are read as UTC
            end=end.strftime("%Y-%m-%dT%H:%M:%S"),
            limit=PAGE,
            order="asc",
            dataset="futures",
        )
        rows += [
            {
                "ts": datetime.fromisoformat(r["timestamp"]).astimezone(UTC),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "contract": None,  # continuous series only; no per-contract field
            }
            for r in page
        ]
        if len(page) < PAGE:
            break
        cursor = rows[-1]["ts"] + timedelta(minutes=1)
    return rows


async def stream_trades(api_key: str, symbols: list[str]) -> AsyncIterator[dict]:
    """Trades as dicts: ts (aware UTC), price, size, symbol."""
    from lse import LSE

    async for t in LSE(api_key=api_key).stream_async(symbols):
        if t.replay or t.datetime is None:
            continue
        yield {
            "ts": t.datetime.astimezone(UTC),
            "price": t.price,
            "size": t.volume or 0,
            "symbol": t.symbol,
        }


# --- pure helpers ---


def weekday_gaps(days: set[date], start: date, end: date) -> list[tuple[date, date]]:
    gaps: list[tuple[date, date]] = []
    run: tuple[date, date] | None = None
    d = start
    while d <= end:
        if d.weekday() < 5:
            if d in days:
                if run:
                    gaps.append(run)
                run = None
            else:
                run = (run[0], d) if run else (d, d)
        d += timedelta(days=1)
    return gaps + ([run] if run else [])


def bucket_ticks(
    ticks: list[dict],
) -> dict[tuple[str, datetime], tuple[float, float, float, float, int]]:
    bars: dict[tuple[str, datetime], tuple[float, float, float, float, int]] = {}
    for t in sorted(ticks, key=lambda t: t["ts"]):
        key = (t["symbol"], t["ts"].replace(second=0, microsecond=0))
        p, s = float(t["price"]), int(t["size"])
        o, h, lo, _, v = bars.get(key, (p, p, p, p, 0))
        bars[key] = (o, max(h, p), min(lo, p), p, v + s)
    return bars


def compare_bars(live: dict, hist: dict, tick: float) -> tuple[int, list[str]]:
    shared = sorted(set(live) & set(hist), key=lambda k: (k[1], k[0]))
    if not shared:
        return 0, ["no overlapping minutes: check symbol, timezone and bar-open/close convention"]
    problems = [
        f"{sym} {ts.isoformat()} {name} live={a} hist={b}"
        for sym, ts in shared
        for name, a, b in zip(
            ("open", "high", "low", "close"),
            live[(sym, ts)][:4],
            hist[(sym, ts)][:4],
            strict=True,
        )
        if abs(a - b) > tick + 1e-9
    ]
    return len(shared), problems


def lag_stats(lags: list[float]) -> tuple[float, float]:
    ordered = sorted(lags)
    return statistics.median(ordered), ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


# --- commands (shell) ---


def cmd_history(args: argparse.Namespace) -> None:
    key = load_settings().lse_api_key
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else datetime.now(UTC)
    rows = fetch_history(key, args.symbol, start, end)
    if not rows:
        print("NO ROWS RETURNED")
        return
    days = {trading_day(r["ts"]) for r in rows}
    print(f"rows={len(rows)} first={rows[0]['ts']} last={rows[-1]['ts']} trading_days={len(days)}")
    print("first rows (raw, check timestamp convention):")
    for r in rows[:3]:
        print("  ", r)
    print("contract codes seen:", sorted({str(r["contract"]) for r in rows}))
    print("weekday gaps (holidays included; mark H or G in docs/data-gate.md):")
    for a, b in weekday_gaps(days, min(days), max(days)):
        print(f"   {a} .. {b}")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"history-{args.symbol}.jsonl"
    out.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    print(f"saved {out}")


async def _record(symbols: list[str], minutes: float, out: Path) -> list[float]:
    key = load_settings().lse_api_key
    stop = time.monotonic() + minutes * 60
    lags: list[float] = []
    with out.open("a") as f:
        async for t in stream_trades(key, symbols):
            lags.append((datetime.now(UTC) - t["ts"]).total_seconds())
            f.write(json.dumps(t, default=str) + "\n")
            if time.monotonic() >= stop:
                break
    return lags


def cmd_live(args: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"ticks-{datetime.now(UTC).date()}.jsonl"
    lags = asyncio.run(_record(args.symbols, args.minutes, out))
    if not lags:
        print("NO TICKS RECEIVED")
        return
    median, p95 = lag_stats(lags)
    verdict = "PASS" if median <= 5 else "FAIL"
    print(f"ticks={len(lags)} lag median={median:.2f}s p95={p95:.2f}s -> {verdict}")
    print(f"saved {out}")


def _load_ticks(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [{**r, "ts": datetime.fromisoformat(r["ts"])} for r in rows]


def cmd_compare(args: argparse.Namespace) -> None:
    ticks = _load_ticks(Path(args.ticks))
    live = bucket_ticks(ticks)
    # drop the first and last minute per symbol: the recording started and stopped mid-minute
    for sym in {k[0] for k in live}:
        minutes = sorted(ts for s, ts in live if s == sym)
        for ts in (minutes[0], minutes[-1]):
            live.pop((sym, ts), None)
    key = load_settings().lse_api_key
    hist: dict = {}
    for sym in sorted({k[0] for k in live}):
        start = min(ts for s, ts in live if s == sym)
        end = max(ts for s, ts in live if s == sym) + timedelta(minutes=1)
        for r in fetch_history(key, sym, start, end):
            hist[(sym, r["ts"])] = (r["open"], r["high"], r["low"], r["close"], r["volume"])
    compared, problems = compare_bars(live, hist, tick=args.tick)
    print(f"compared={compared} mismatches={len(problems)}")
    for p in problems[:50]:
        print("  ", p)
    print("PASS" if compared and not problems else "FAIL")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(required=True)
    h = sub.add_parser("history", help="items 1-2: coverage, gaps, contract codes")
    h.add_argument("symbol")
    h.add_argument("--start", default="2000-01-01")
    h.add_argument("--end")
    h.set_defaults(func=cmd_history)
    lv = sub.add_parser("live", help="item 3: record live trades, report lag")
    lv.add_argument("symbols", nargs="+")
    lv.add_argument("--minutes", type=float, default=10)
    lv.set_defaults(func=cmd_live)
    c = sub.add_parser("compare", help="item 4: live-built bars vs historical bars")
    c.add_argument("ticks")
    c.add_argument("--tick", type=float, default=0.25)
    c.set_defaults(func=cmd_compare)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
