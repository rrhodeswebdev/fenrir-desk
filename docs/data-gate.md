# LSE Data Gate — Findings

Spec: `SPEC.md` → Market data, "Data gate".
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

Result: PASS
- ES (`ES.F`): first bar 2016-05-29 22:01 UTC (trading day 2016-05-30), last 2026-10-09 20:59 UTC, 3,636,937 rows, 2,679 trading days.
- NQ (`NQ.F`): same span, 3,613,644 rows, 2,678 trading days.
- Weekday gaps, both series: 2016-12-26 H, 2017-01-02 H, 2017-04-14 H, 2017-12-25 H, 2018-01-01 H, 2018-03-30 H, 2018-12-25 H, 2019-01-01 H, 2019-04-19 H, 2019-12-25 H, 2020-01-01 H, 2020-04-10 H, 2020-12-25 H, 2021-01-01 H, 2021-12-24 H, 2022-04-15 H, 2022-12-26 H, 2023-01-02 H, 2023-12-25 H, 2024-01-01 H, 2024-03-29 H, 2024-12-25 H, 2025-01-01 H, 2025-04-18 H, 2025-12-25 H, 2026-01-01 H (Christmas, New Year, Good Friday; other holidays have shortened sessions, so they are present).
- NQ only: 2020-02-28 G (one whole Friday missing). One trading day, under the 2-day limit.
- Not checked: gaps inside a session (missing minutes). The gap list is whole days only.
- Timestamps: bar OPEN, UTC. Session-open bars at 18:00 ET on 2,637 of 2,679 ES days; the close side has a 16:59 ET bar on 2,584 days and a 17:00 ET bar on only 11. No shift needed.
- Pull cost: about 6 minutes and 3 GB peak memory per series via 5000-row `candles()` pages; plan 03 should stream pages to Parquet (or use `history()` exports) rather than hold every row.

## Item 2 — Per-contract or continuous; unadjusted front-month available

Result: PASS, with a roll-date mismatch that plan 03 must handle (see below)
- History is: continuous only (`ES.F`, `NQ.F`). Per-contract symbols return no rows.
- Unadjusted front-month prices available: yes. Evidence: `ES.F` minus spot `SPX500/USD` (and `NQ.F` minus `NAS100/USD`) at 15:00 ET falls toward 0 as expiry nears, then jumps by one quarter's carry at the roll and stays there. Examples: ES 2026-06-12 basis 2.65 → 2026-06-15 74.74; NQ 2025-12-12 15.42 → 2025-12-15 269.24. A back-adjusted series would show no jump. The 2016 levels (ES 2091.50 on 2016-05-29) also match the actual June 2016 contract, not a level shifted by ten years of roll spreads.
- **LSE rolls later than `instruments.yaml`.** For 14 of the 15 quarterly rolls from 2023-03 to 2026-09, the basis jump falls at the start of expiry week: the Sunday 18:00 ET open (trading day Monday), or for a few rolls a day later (ES 2026-09, NQ 2024-12, 2025-03, 2026-06, 2026-09). The exception, ES 2023-03, is ambiguous: its largest jump (16 points) is at the Friday cash close, inside spot-only RTH data. Our calendar rolls on the Thursday 8 days before expiry. So for 2–3 trading days each quarter (Thu and Fri, plus Mon when LSE rolls on the Tuesday trading day), `ES.F`/`NQ.F` prices come from the expiring contract while `front_month()` names the next one. The price gap is the roll spread: about 15–80 ES points and 60–330 NQ points in 2023–2026 (larger when rates are higher).
- Before 2023 the roll date can't be checked this way (spot history starts 2023-02).

## Roll mismatch — options for plan 03

**Chosen (2026-10-10): option 2, with a fixed calendar window.** The skipped days are every trading day from the `instruments.yaml` roll day (the Thursday 8 days before expiry) through the Tuesday of expiry week, inclusive: Thu, Fri, Mon, Tue, so 4 trading days per quarterly roll, 16 a year. The observed mismatch is Thu, Fri and sometimes Mon; Tuesday is a deliberate one-day safety margin for rolls we cannot check. ES 2023-03 is unresolved (the doc does not pin which Friday its jump falls on). The window needs no roll detection in the data, which before 2023 would be guesswork (no spot history to measure the basis against). Backtests skip those days and live trading takes no entries on them, so both treat them the same.

1. **Follow LSE's roll.** Find each roll in the data (the basis jump against spot from 2023, or a detected price jump earlier) and label bars with what LSE actually holds. Backtests stay right. Live trading then needs a rule for which contract NinjaTrader trades during those 2–3 days.
2. **Keep the CME calendar and drop the mismatch days.** Bars between our roll and LSE's are marked unusable: skipped in backtests, no trading live. 8–12 trading days a year as observed (16 with the chosen safety margin).
3. **Move `instruments.yaml` to LSE's convention** (roll at the start of expiry week). Simple, but the expiring contract has thin volume that week, and LSE's roll day varies between Monday and Tuesday.

## Item 3 — Live WebSocket is real-time ES and NQ

Result: NO TICKS RECEIVED (a fail: the current LSE tier streams no futures)
- Run 2026-10-11 (Sunday), Globex open (ES and NQ trade from 18:00 ET): `live ES.F NQ.F --minutes 30` from 18:10:27 ET. 0 ticks for `ES.F` and `NQ.F`, so 0 without volume; lag n/a. `data/gate/ticks-2026-10-11T221027.jsonl` is empty. The Mac idle-slept from 18:21:07 to 18:50:12 ET (`pmset -g log`), so about 10.5 minutes were actually recorded; the process was killed at 18:50 before it printed its summary (see the run note below).
- Re-checks the same evening, Globex still open:
  - 18:53 ET, 60 s, subscribed to `ES.F`, `NQ.F`, `MES.F`, `MNQ.F`, `ESZ26`, `ESZ6`, `NQZ26`, `NQZ6`, `ES`, `NQ`, `MES`, `MNQ`, `/ES`, `/NQ`: 0 ticks and no error reply (the server ignores a symbol it does not serve).
  - 18:56–18:58 ET, `live ES.F NQ.F BTC/USD --minutes 2`: 121 ticks, all `BTC/USD`, 0 for `ES.F`/`NQ.F`; ended on time (120 s). So the socket and the script work and futures are simply not served. Control lag: median −0.53 s, p95 −0.52 s with the local clock 0.64 s slow (`sntp time.apple.com`), so about +0.1 s.
- Tier and streamable symbols (the socket's `authenticated` message, 2026-10-11): tier `registered`, 4,173 symbols: stocks 3,987, forex 62, crypto 58, ETFs 25, commodities 23 (spot, e.g. `WTICO/USD`, `XAU/USD`), indices 13 (spot, e.g. `SPX500/USD`, `NAS100/USD`), 5 uncategorised (`BNKE.L`, `USB02Y/USD` … `USB30Y/USD`). No futures category and no ES, NQ, MES or MNQ in any form; the one `ES` is Eversource Energy stock. 2026-10-10 showed the same (tier `registered`, 4,169 symbols). The spot indices are not a stand-in: their price differs from the future by the basis (see Item 2).
- Contracts streamed: none.
- A weekday regular-hours re-run would show the same unless the tier changes: the symbol list, not the session, is what leaves futures out.
- Run note: the "hang" after the deadline was the sleep, not the SDK. `time.monotonic` (`mach_absolute_time` on macOS) stops while the machine sleeps, and the deadline runs on it, so about 19 of the 30 minutes were still left at 18:50. The 2-minute run above closed the stream on time. `live` now holds a `caffeinate -i` assertion on macOS for as long as it runs; closing the lid still sleeps the machine.

## Item 4 — A day of live bars matches historical bars

Result: PASS / FAIL
- Day: `____`. Minutes compared: `____`. Mismatches beyond 1 tick: `____`.
- Notes: ...

## Item 5 — LSE prices vs NinjaTrader sim fills

Deferred to plan 22 (needs the add-on and a sim account).

## Values downstream plans need

| Value | Used by | Answer |
|---|---|---|
| Default `fenrir data pull --since` (earliest usable day) | 03 | `2016-06-01` (first full month; data starts 2016-05-29 evening) |
| LSE symbol for our code `NQZ26` | 03, 04 | None per contract. History uses `NQ.F` / `ES.F` (continuous, unadjusted, LSE's own roll dates; see "Roll mismatch") |
| Timestamp shift to bar OPEN in UTC | 03 | none (timestamps are already bar open, UTC) |
| Roll-window days to skip | 03, 08, 21 | Every trading day from the `instruments.yaml` roll day (third Friday − 8 days, a Thursday) through the Tuesday of expiry week, inclusive |
| Live message → `Tick(ts, price, size, contract)` mapping | 04 | Cannot be filled: no futures ticks arrive on the current tier. From the SDK and the `BTC/USD` control only: `tick.datetime` (aware UTC) → `ts`, `price` → `price`, `volume` (float, may be `None`) → `size`; the message has no contract field. To be filled for the source DEC-7 picks |

## Decision

- Items 1–2: PASS → plan 03 may run. FAIL → stop; revisit the data source (Databento is the known alternative) before plan 03.
- Items 3–4: PASS → plans 04, 21 may run. FAIL → live trading blocked until the user picks another live source; plans 03, 05–18 continue.

Decision: Items 1–2 PASS → plan 03 may run, skipping the roll-window days (option 2 above). Item 3 NO TICKS RECEIVED (2026-10-11): LSE's `registered` tier streams no futures, so item 4 cannot run on it either. DEC-7 (#18, live data source) is now needed; plans 04.2, 21.5, 21.8 and 22.7 stay blocked until it is decided. Plans 03 and 05–18 continue.
