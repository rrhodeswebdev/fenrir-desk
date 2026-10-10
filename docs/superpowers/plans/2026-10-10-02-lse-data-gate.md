# LSE Data Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Using a real London Strategic Edge (LSE) API key, check spec §3 data-gate items 1–4, record the results and the real `lse-data` client API in `docs/data-gate.md`, and make the go/no-go call for plans 03, 04, 21 and 22.

**Architecture:** One throwaway script, `scripts/lse_gate.py`, with three subcommands (`history`, `live`, `compare`). Its comparison logic is pure functions, unit-tested; the LSE calls are confined to two functions at the top of the file, written against the *expected* API and corrected in Task 1 once the real API is known. Most of the work is manual: running the script with a real key and writing down what it shows. Item 5 (LSE vs NinjaTrader fill prices) needs the add-on and runs in plan 22.

**Tech Stack:** Python 3.12, `uv`, `lse-data` (London Strategic Edge Python client; loaded with `uv run --with lse-data`, not added to the app's dependencies here), `fenrir.core` from plan 01.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§3 Data gate, §13 Risks)
**Index:** `docs/superpowers/plans/README.md`

## Global Constraints

- The gate runs **before any live-trading work** and before plan 03 is executed.
- Items checked: (1) ES and NQ 1-minute history exists — how far back and any gaps; (2) per-contract or continuous, and whether unadjusted front-month prices are available; (3) the live WebSocket streams real-time (not delayed) ES and NQ; (4) a day of LSE live bars matches LSE historical bars for the same minutes.
- Decision rule (verbatim from spec): **If 1–2 fail, the backtester is not trustworthy and the data source is revisited (Databento is the known alternative). If 3–5 fail, live trading is blocked until the user decides on another live source.**
- The fetch step is the only code that knows about LSE (`fenrir.data.lse` from plan 03 onward). This script is not imported by the app.
- Prices for signals and orders must be unadjusted front-month, never back-adjusted continuous.
- The LSE API key lives in `.env` as `FENRIR_LSE_API_KEY`; never paste it into `docs/data-gate.md` or a commit.
- Directory context file rule: `scripts/` gets a `CLAUDE.md` and a Directory Map row in the commit that creates it.
- Commits: Conventional Commits, scope `data`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **"0 compared" is not a pass**: if live and historical bars share no minutes (wrong symbol, wrong timezone, wrong day), `compare` must say so and fail, not print "all match". Pinned in Task 2 (`test_compare_bars_no_overlap_is_not_a_pass`).
2. **Timestamp convention**: LSE may stamp bars by close time or in exchange-local time; a one-minute or one-hour shift makes every comparison wrong. The script prints raw first rows and the session-open minute; Task 3 records the convention explicitly.
3. **Holidays look like gaps**: weekday gaps include CME holidays (Thanksgiving, Christmas, Good Friday); they must be listed, not hidden, and the human marks which are holidays. Pinned in Task 2 (`test_weekday_gaps_collapses_runs_and_skips_weekends`).
4. **Delayed feed passing as live**: a 10- or 15-minute delayed stream still delivers ticks. The `live` command reports median and p95 lag versus wall clock; above 5 s median is a fail. Pinned in Task 2 (`test_lag_stats`).
5. **Out-of-order ticks**: ticks arriving out of order must not corrupt the minute's open/close in the comparison bars. Pinned in Task 2 (`test_bucket_ticks_orders_by_time`).

---

### Task 1: Discover the real `lse-data` API and start the findings doc

**Files:**
- Create: `docs/data-gate.md`

**Interfaces:**
- Consumes: an LSE account and API key (user-provided, in `.env`).
- Produces: `docs/data-gate.md` "Client API" section, read by plans 03, 04, 21, 22 before they touch LSE code.

- [ ] **Step 1: Confirm the key is configured**

Run: `grep -c '^FENRIR_LSE_API_KEY=.\+' .env`
Expected: `1`. If `0` or `.env` is missing, ask the user to `cp .env.example .env` and add their LSE key. Do not continue without it.

- [ ] **Step 2: Install and inspect the client**

```bash
uv run --with lse-data python -c "import importlib.metadata as m; print(m.metadata('lse-data')['Name'], m.version('lse-data')); print(m.metadata('lse-data').get('Home-page') or m.metadata('lse-data').get_all('Project-URL'))"
uv run --with lse-data python -c "import lse_data, pkgutil; print(lse_data.__file__); print([n for n in dir(lse_data) if not n.startswith('_')]); print([m.name for m in pkgutil.iter_modules(lse_data.__path__)])"
```

Expected: the package name, version, project URL, and the public names in `lse_data`. If the import name is not `lse_data`, list the installed top-level modules with `uv run --with lse-data python -c "import importlib.metadata as m; print(m.distribution('lse-data').read_text('top_level.txt'))"` and use that name instead.

- [ ] **Step 3: Read the docs and signatures for history and streaming**

```bash
uv run --with lse-data python -c "import lse_data, inspect; [print(n, inspect.signature(getattr(lse_data, n)) if callable(getattr(lse_data, n)) else '') for n in dir(lse_data) if not n.startswith('_')]"
uv run --with lse-data python -c "import lse_data; help(lse_data)" | head -200
```

Also open the project URL from Step 2. Find: the client class and how it takes the API key; the historical bars call (symbol format, interval argument, start/end types, return type and field names, timestamp timezone and whether it marks bar open or close); whether a symbol can name one contract (e.g. `NQZ6`/`NQZ26`) or only a continuous series, and whether continuous is back-adjusted; the live streaming call (sync or async, trades or bars, field names); rate limits.

- [ ] **Step 4: Write `docs/data-gate.md`**

Fill in the Client API section from Steps 2–3. Leave the item sections as shown; they are filled in Tasks 3–4.

```markdown
# LSE Data Gate — Findings

Spec: `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` §3 "Data gate".
Run with: `uv run --with lse-data python scripts/lse_gate.py <command>`.
Date run: YYYY-MM-DD. Run by: <name>. `lse-data` version: <x.y.z>.

## Client API

| Question | Answer |
|---|---|
| Install name / import name | `lse-data` / `<module>` |
| Client construction and auth | `<e.g. Client(api_key=...)>` |
| History call | `<exact call with argument names and types>` |
| History return type and fields | `<type; field names for time, open, high, low, close, volume, contract>` |
| History timestamp: timezone; bar open or close | `<UTC/exchange-local>; <open/close>` |
| Symbol format for one contract | `<e.g. NQZ6, NQZ26, NQ.Z26>` or "not supported" |
| Continuous series available? Back-adjusted? | `<yes/no>; <adjusted/unadjusted>` |
| Live call (sync/async), message type, fields | `<exact call; trades or bars; field names>` |
| Rate limits / max range per request | `<...>` |
| Docs URL | `<...>` |

## Item 1 — ES and NQ 1-minute history exists

Result: PASS / FAIL
- ES: first day `____`, last day `____`, trading days `____`.
- NQ: first day `____`, last day `____`, trading days `____`.
- Weekday gaps (holidays marked `H`, real gaps marked `G`): ...

## Item 2 — Per-contract or continuous; unadjusted front-month available

Result: PASS / FAIL
- History is: per-contract / continuous / both.
- Unadjusted front-month prices available: yes / no. Evidence (prices either side of a roll): ...

## Item 3 — Live WebSocket is real-time ES and NQ

Result: PASS / FAIL
- Lag vs wall clock over `__` minutes: median `__` s, p95 `__` s (fail if median > 5 s).
- Contracts streamed: ...

## Item 4 — A day of live bars matches historical bars

Result: PASS / FAIL
- Day: `____`. Minutes compared: `____`. Mismatches beyond 1 tick: `____`.
- Notes: ...

## Item 5 — LSE prices vs NinjaTrader sim fills

Deferred to plan 22 (needs the add-on and a sim account).

## Values downstream plans need

| Value | Used by | Answer |
|---|---|---|
| Default `fenrir data pull --since` (earliest usable day) | 03 | `____` |
| LSE symbol for our code `NQZ26` | 03, 04 | `____` |
| Timestamp shift to bar OPEN in UTC | 03 | `____` |
| Live message → `Tick(ts, price, size, contract)` mapping | 04 | `____` |

## Decision

- Items 1–2: PASS → plan 03 may run. FAIL → stop; revisit the data source (Databento is the known alternative) before plan 03.
- Items 3–4: PASS → plans 04, 21 may run. FAIL → live trading blocked until the user picks another live source; plans 03, 05–18 continue.

Decision: ____
```

- [ ] **Step 5: Commit**

```bash
git add docs/data-gate.md
git commit -m "$(cat <<'EOF'
docs(data): record lse-data client api for the data gate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Gate script with tested comparison logic

**Files:**
- Create: `scripts/lse_gate.py`
- Create: `scripts/CLAUDE.md`
- Test: `tests/scripts/test_lse_gate.py`
- Modify: `CLAUDE.md` (Directory Map table)

**Interfaces:**
- Consumes: `fenrir.core.sessions.trading_day`, `fenrir.settings.load_settings` (plan 01).
- Produces: nothing other plans import. Pure helpers inside the script: `weekday_gaps(days: set[date], start: date, end: date) -> list[tuple[date, date]]`, `bucket_ticks(ticks: list[dict]) -> dict[tuple[str, datetime], tuple[float, float, float, float, int]]`, `compare_bars(live, hist, tick: float) -> tuple[int, list[str]]`, `lag_stats(lags: list[float]) -> tuple[float, float]`.

- [ ] **Step 1: Write the failing tests**

`tests/scripts/test_lse_gate.py`:

```python
import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

_path = Path(__file__).parents[2] / "scripts" / "lse_gate.py"
_spec = importlib.util.spec_from_file_location("lse_gate", _path)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_weekday_gaps_collapses_runs_and_skips_weekends():
    # Mon 2026-10-05 .. Fri 2026-10-16; missing Thu 8, Fri 9, Mon 12 (weekend between) and Wed 14
    present = {date(2026, 10, d) for d in (5, 6, 7, 13, 15, 16)}
    assert gate.weekday_gaps(present, date(2026, 10, 5), date(2026, 10, 16)) == [
        (date(2026, 10, 8), date(2026, 10, 12)),
        (date(2026, 10, 14), date(2026, 10, 14)),
    ]


def test_weekday_gaps_none():
    present = {date(2026, 10, 5), date(2026, 10, 6)}
    assert gate.weekday_gaps(present, date(2026, 10, 5), date(2026, 10, 6)) == []


def test_bucket_ticks_orders_by_time():
    ticks = [
        {"ts": utc(2026, 10, 8, 14, 30, 50), "price": 103.0, "size": 1, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 30, 1), "price": 100.0, "size": 2, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 30, 20), "price": 99.0, "size": 1, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 31, 0), "price": 104.0, "size": 5, "symbol": "NQZ6"},
    ]
    assert gate.bucket_ticks(ticks) == {
        ("NQZ6", utc(2026, 10, 8, 14, 30)): (100.0, 103.0, 99.0, 103.0, 4),
        ("NQZ6", utc(2026, 10, 8, 14, 31)): (104.0, 104.0, 104.0, 104.0, 5),
    }


def test_compare_bars_within_one_tick_matches():
    key = ("NQZ6", utc(2026, 10, 8, 14, 30))
    live = {key: (100.0, 103.0, 99.0, 103.0, 4)}
    hist = {key: (100.25, 103.0, 99.0, 102.75, 9)}
    assert gate.compare_bars(live, hist, tick=0.25) == (1, [])


def test_compare_bars_reports_mismatch():
    key = ("NQZ6", utc(2026, 10, 8, 14, 30))
    live = {key: (100.0, 103.0, 99.0, 103.0, 4)}
    hist = {key: (100.0, 104.0, 99.0, 103.0, 4)}
    compared, problems = gate.compare_bars(live, hist, tick=0.25)
    assert compared == 1
    assert problems == ["NQZ6 2026-10-08T14:30:00+00:00 high live=103.0 hist=104.0"]


def test_compare_bars_no_overlap_is_not_a_pass():
    live = {("NQZ6", utc(2026, 10, 8, 14, 30)): (1.0, 1.0, 1.0, 1.0, 1)}
    hist = {("NQZ6", utc(2026, 10, 8, 18, 30)): (1.0, 1.0, 1.0, 1.0, 1)}
    compared, problems = gate.compare_bars(live, hist, tick=0.25)
    assert compared == 0
    assert problems == ["no overlapping minutes: check symbol, timezone and bar-open/close convention"]


def test_lag_stats():
    assert gate.lag_stats([0.25, 0.75, 0.5, 9.0]) == (0.625, 9.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scripts/test_lse_gate.py`
Expected: FAIL with `FileNotFoundError` for `scripts/lse_gate.py`.

- [ ] **Step 3: Write the script**

`scripts/lse_gate.py`:

```python
"""LSE data gate (spec §3 items 1-4). Throwaway; not imported by the app.

Run: uv run --with lse-data python scripts/lse_gate.py history ES --start 2010-01-01
     uv run --with lse-data python scripts/lse_gate.py live NQZ6 ESZ6 --minutes 10
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

# --- EXPECTED API: replace these two functions with the calls recorded in docs/data-gate.md ---


def fetch_history(api_key: str, symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Rows as dicts: ts (aware UTC, bar OPEN), open, high, low, close, volume, contract (or None)."""
    from lse_data import Client  # EXPECTED API

    client = Client(api_key=api_key)
    return [
        {
            "ts": r.timestamp.astimezone(UTC),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "contract": getattr(r, "contract", None),
        }
        for r in client.history(symbol=symbol, interval="1m", start=start, end=end)
    ]


async def stream_trades(api_key: str, symbols: list[str]) -> AsyncIterator[dict]:
    """Trades as dicts: ts (aware UTC), price, size, symbol."""
    from lse_data import AsyncClient  # EXPECTED API

    async with AsyncClient(api_key=api_key) as client:
        async for t in client.stream_trades(symbols=symbols):
            yield {"ts": t.timestamp.astimezone(UTC), "price": t.price, "size": t.size, "symbol": t.symbol}


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


def bucket_ticks(ticks: list[dict]) -> dict[tuple[str, datetime], tuple[float, float, float, float, int]]:
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
        for name, a, b in zip(("open", "high", "low", "close"), live[(sym, ts)][:4], hist[(sym, ts)][:4], strict=True)
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
    print(f"ticks={len(lags)} lag median={median:.2f}s p95={p95:.2f}s -> {'PASS' if median <= 5 else 'FAIL'}")
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
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
```

If Task 1 found a different API, edit only `fetch_history` and `stream_trades` (keep their return shapes) and note the change in `docs/data-gate.md`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/scripts/test_lse_gate.py`
Expected: `7 passed`. (The tests never import `lse_data`; the imports sit inside the two API functions.)

- [ ] **Step 5: Add `scripts/CLAUDE.md` and the Directory Map row**

`scripts/CLAUDE.md`:

```markdown
# scripts/

One-off and maintenance scripts run by hand. The app never imports anything here.

- Entry points: `lse_gate.py` (spec §3 data gate; results in `docs/data-gate.md`).
- Pure vs shell: pure helpers are unit-tested in `tests/scripts/`; provider calls sit in clearly marked functions at the top of each script.
- Not here: app code (`src/fenrir/`); the app's LSE fetch lives in `src/fenrir/data/lse.py`.
- Outputs go to `data/gate/` (gitignored).
```

Append to the end of the table in root `CLAUDE.md` → "## 6. Directory Map":

```markdown
| `scripts/` | One-off scripts run by hand (LSE data gate). Never imported by the app. |
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format . && uv run ruff check .
git add scripts tests/scripts CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(data): add lse data gate script

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

Expected: `All checks passed!`.

---

### Task 3: Run items 1–2 (history) and record them

**Files:**
- Modify: `docs/data-gate.md` (Items 1, 2, "Values downstream plans need")

**Interfaces:**
- Consumes: `scripts/lse_gate.py history`.
- Produces: Item 1–2 results and the go/no-go for plan 03.

- [ ] **Step 1: Pull continuous ES and NQ history**

Use the continuous/root symbol found in Task 1 (shown here as `ES` / `NQ`):

```bash
uv run --with lse-data python scripts/lse_gate.py history ES --start 2000-01-01
uv run --with lse-data python scripts/lse_gate.py history NQ --start 2000-01-01
```

Expected: `rows=... first=... last=... trading_days=...`, three raw rows, contract codes seen, and a gap list. If `NO ROWS RETURNED`, try the symbol formats from the docs before recording FAIL.

- [ ] **Step 2: Check the timestamp convention**

From the raw rows, find the Sunday/weekday session-open bar. The first bar of a session must be at 22:00 UTC (EDT) or 23:00 UTC (EST), i.e. 18:00 New York. If it is at 18:01 New York, LSE stamps bar close: record "shift −1 minute to bar open". If it is offset by whole hours, LSE uses exchange-local time without zone: record the conversion. Write the result in "Values downstream plans need".

- [ ] **Step 3: Check per-contract and unadjusted prices (item 2)**

Pull one contract across its last days as front month and the next contract, using the per-contract symbol format from Task 1 (example: December 2025 roll on 2025-12-11):

```bash
uv run --with lse-data python scripts/lse_gate.py history NQZ5 --start 2025-12-08 --end 2025-12-13
uv run --with lse-data python scripts/lse_gate.py history NQH6 --start 2025-12-08 --end 2025-12-13
```

Expected: both return rows; NQH6 trades above NQZ5 by the roll spread (tens of NQ points). Compare the continuous series' closes on 2025-12-10 and 2025-12-11 (from `data/gate/history-NQ.jsonl`) against these: if the continuous series equals NQZ5 before the roll and NQH6 after, with a jump, it is unadjusted front-month; if old prices are shifted by the spread, it is back-adjusted. Item 2 PASSES if per-contract history exists for at least the tuning period, or the continuous series is unadjusted front-month.

- [ ] **Step 4: Fill in Items 1 and 2 and commit**

Mark each weekday gap `H` (CME holiday: New Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, Independence, Labor, Thanksgiving, Christmas) or `G` (real gap). Item 1 PASSES if both series cover at least 5 years with no `G` gaps longer than 2 trading days. Record the earliest usable day as the default `--since`.

```bash
git add docs/data-gate.md
git commit -m "$(cat <<'EOF'
docs(data): record data gate history results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Decide for items 1–2**

If Item 1 or 2 is FAIL: write "Decision: STOP — revisit data source (Databento)" in `docs/data-gate.md`, commit, and tell the user. Plans 03, 04, 08–10, 21, 22 must not start. Otherwise continue.

---

### Task 4: Run items 3–4 (live) and record the decision

**Files:**
- Modify: `docs/data-gate.md` (Items 3, 4, Decision)

**Interfaces:**
- Consumes: `scripts/lse_gate.py live` and `compare`.
- Produces: the final Decision line read by plans 03, 04, 21, 22.

- [ ] **Step 1: Record live trades for 10 minutes during regular hours (item 3)**

Run on a weekday between 09:30 and 16:00 New York, with the current front-month symbols in LSE format:

```bash
uv run --with lse-data python scripts/lse_gate.py live NQZ6 ESZ6 --minutes 10
```

Expected: `ticks=<thousands> lag median=<s> p95=<s> -> PASS`. A median near 600 s or 900 s means a 10- or 15-minute delayed feed: FAIL. `NO TICKS RECEIVED` during market hours: FAIL.

- [ ] **Step 2: Record a full session (item 4)**

Start before 18:00 New York and let it run until 17:00 the next day (23 hours):

```bash
uv run --with lse-data python scripts/lse_gate.py live NQZ6 ESZ6 --minutes 1380
```

- [ ] **Step 3: Compare with historical bars (item 4)**

Wait at least one hour after the session ends (history may publish late), then:

```bash
uv run --with lse-data python scripts/lse_gate.py compare data/gate/ticks-YYYY-MM-DD.jsonl
```

Expected: `compared=<~2700> mismatches=0` and `PASS`. Item 4 PASSES with zero mismatches beyond 1 tick and at least 90% of session minutes compared. If `no overlapping minutes`, recheck the timestamp convention from Task 3 Step 2 before recording FAIL.

- [ ] **Step 4: Fill in Items 3–4 and the Decision, then commit**

Fill the live-message mapping in "Values downstream plans need", then the Decision line, e.g. `Decision: GO for 03 and 04; item 5 pending plan 22.` or `Decision: GO for 03; live BLOCKED (item 3 failed: 15-minute delay).`

```bash
git add docs/data-gate.md
git commit -m "$(cat <<'EOF'
docs(data): record data gate live results and decision

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Tell the user the decision**

Report the four results and the Decision line. If anything is blocked, ask the user which source to evaluate next; do not start plan 03 or 04 until they answer.
