# LSE Data Gate — Findings

Spec: `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` §3 "Data gate".
Run with: `uv run --with lse-data python scripts/lse_gate.py <command>`.
Date run: 2026-10-10. Run by: Claude (for Ryan Rhodes). `lse-data` version: 0.14.0.

## Client API

| Question | Answer |
|---|---|
| Install name / import name | `lse-data` / `lse` (not `lse_data`) |
| Client construction and auth | `from lse import LSE, LSEError`; `LSE(api_key=...)` (or env `LSE_API_KEY`). Failed calls raise `LSEError(status, message)` |
| History call | `client.candles(symbol: str, timeframe: str = "1m", start: str \| None, end: str \| None, limit: int = 5000, order: "asc"\|"desc", dataset: str \| None) -> list[dict]`. `start`/`end` are ISO strings filtering bar time. One page per call (max 5000 rows); page with `start`/`end`. Bulk: `client.history(symbol, timeframe="1m", start=..., dataset="futures", dataframe=False)` runs a vault export job and downloads Parquet (5 exports/hour) |
| History return type and fields | `list[dict]`: `symbol`, `open`, `high`, `low`, `close`, `volume`, `timestamp`. No contract field. Prices carry float noise (`7827.250000000001`): round to tick. `volume` is float |
| History timestamp: timezone; bar open or close | ISO string with `Z` (UTC). SDK comment: the vault labels the bar-open time. Verified in Task 3 |
| Symbol format for one contract | Not supported: `ESZ6`, `ESZ26`, `ESZ2026`, `ES.Z26`, `ESZ6.F` all return 0 rows; the catalog lists no per-contract CME index futures |
| Continuous series available? Back-adjusted? | Yes: `ES.F`, `NQ.F` (dataset `futures`). Adjustment checked in Task 3 |
| Live call (sync/async), message type, fields | `async for tick in client.stream_async(symbols, reconnect=True, start=None)` (sync: `client.stream(...)`). Yields `Tick`: `symbol`, `price`, `bid`, `ask`, `volume`, `timestamp` (ISO string; `tick.datetime` parses it), `name`, `replay`. Trades/quotes, not bars. `start=` replays up to 24 h then goes live |
| Rate limits / max range per request | `GET /vault/usage`: 200 calls/min, 5000 rows/request, 5 exports/hour, vault concurrency 2, 15 GiB/week, 50 GiB/month; `historical_data_months: -1` (unlimited) |
| Docs URL | https://github.com/londonstrategicedge/lse-data |

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
