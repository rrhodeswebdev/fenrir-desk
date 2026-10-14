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
| Rate limits / max range per request | `GET /vault/usage`: 200 calls/min, 5000 rows/request, 5 exports/hour, vault concurrency 2, 15 GiB/week, 50 GiB/month; `historical_data_months: -1` (unlimited). WebSocket: 1 connection per key (2026-10-13: a second one gets `Max 1 concurrent websocket connection(s) for this API key`, which the SDK treats as fatal and ends `stream_async`) |
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

Result: PASS (2026-10-13). The 2026-10-11 run below received no futures ticks. The 2026-10-13 run used the owner's own LSE key, now in `.env` on the Linux machine; that key (or its tier) is what changed, but the tier was not re-read (see below).
- Run 2026-10-13 (Tuesday), regular hours: `live ES.F NQ.F --minutes 2` from 14:23:20 UTC (10:23 ET): `ticks=3210 (no volume: 0) lag median=0.25s p95=0.55s -> PASS` (clock NTP-synchronised), saved `data/gate/ticks-2026-10-13T142320.jsonl`.
  - `NQ.F` 1,836 ticks (30765.75–30795.0), `ES.F` 1,374 ticks (7849.75–7853.25). Every tick has `ts`, `price`, `size`, `bid`, `ask`, `symbol`; every price is on the 0.25 tick.
  - `volume` looks like the per-trade quantity, not a running total: 1 on 2,082 of the 3,210 ticks, then 2, 3, 4 …; consecutive sizes go up and down. Inferred from the values only: history for these minutes was not yet published to compare against (ES 14:24 UTC sums to 2,901, NQ to 1,582). No tick had a missing or zero volume. Item 4 confirmed it against history.
  - `price == bid` on every tick and never `== ask`, in this file (3,210) and in the item 4 recording (28,376 read on 2026-10-13). Real trades print at both sides, so LSE's `price` may be the bid rather than the last trade (or `bid` is filled with the last trade). Spread: ES 0.25 here (0.5 on 45 of 13,175 ES ticks in the item 4 file), so ES bars stay within 1 tick either way; NQ 0.25–1.25 nearly always (0.5 on 918, 0.75 on 472 here; up to 7.0 rarely), so bid-built NQ bars may miss the 1-tick tolerance. Item 4's `compare` showed it does not matter: no NQ minute was off by more than 1 tick.
  - Tier not re-read: the socket allows one connection per key (see Client API), and the item 4 recording, then the #82 dry run, held it. The tier and symbol list are in the `authenticated` message the next time the socket is free.
  - Contracts streamed: `ES.F`, `NQ.F` (continuous series, as in history).
- `stream_bars` on live data (04.3, #74), 2026-10-14 09:46–09:50 ET, after the item 4 recording had freed the socket: 8 closed bars for `NQZ26` and `ESZ26` (today's front months), 13:46–13:49 UTC, each delivered 0.14–0.37 s after its minute ended. The first bar of each was partial because the subscription started mid-minute; the live runner subscribes with `start` at the session open, so its first bars are whole.
- Run 2026-10-11 (Sunday), Globex open (ES and NQ trade from 18:00 ET): `live ES.F NQ.F --minutes 30` from 18:10:27 ET. 0 ticks for `ES.F` and `NQ.F`, so 0 without volume; lag n/a. `data/gate/ticks-2026-10-11T221027.jsonl` is empty. The Mac idle-slept from 18:21:07 to 18:50:12 ET (`pmset -g log`), so about 10.7 minutes were actually recorded; the process was killed at 18:50 before it printed its summary (see the run note below).
- Re-checks the same evening, Globex still open:
  - 18:53 ET, 60 s, subscribed to `ES.F`, `NQ.F`, `MES.F`, `MNQ.F`, `ESZ26`, `ESZ6`, `NQZ26`, `NQZ6`, `ES`, `NQ`, `MES`, `MNQ`, `/ES`, `/NQ`: 0 ticks and no error reply (the server ignores a symbol it does not serve).
  - 18:56–18:58 ET, `live ES.F NQ.F BTC/USD --minutes 2`: 121 ticks, all `BTC/USD`, 0 for `ES.F`/`NQ.F`; ended on time (120 s). So the socket and the script work and futures are simply not served. Control lag: median −0.53 s, p95 −0.52 s with the local clock 0.64 s slow (`sntp time.apple.com`), so about +0.1 s.
- Tier and streamable symbols (the socket's `authenticated` message, 2026-10-11): tier `registered`, 4,173 symbols: stocks 3,987, forex 62, crypto 58, ETFs 25, commodities 23 (spot, e.g. `WTICO/USD`, `XAU/USD`), indices 13 (spot, e.g. `SPX500/USD`, `NAS100/USD`), 5 uncategorised (`BNKE.L`, `USB02Y/USD` … `USB30Y/USD`). No futures category and no ES, NQ, MES or MNQ in any form; the one `ES` is Eversource Energy stock. 2026-10-10 showed the same (tier `registered`, 4,169 symbols). The spot indices are not a stand-in: their price differs from the future by the basis (see Item 2).
- Contracts streamed: none.
- A weekday regular-hours re-run would show the same unless the tier changes: the symbol list, not the session, is what leaves futures out. (2026-10-13: a re-run with the owner's key streamed both, see above.)
- Run note: the "hang" after the deadline was the sleep, not the SDK. `time.monotonic` (`mach_absolute_time` on macOS) stops while the machine sleeps, and the deadline runs on it, so about 19 of the 30 minutes were still left at 18:50. The 2-minute run above closed the stream on time. `live` now holds a `caffeinate -i` assertion on macOS for as long as it runs; closing the lid still sleeps the machine.

## Item 4 — A day of live bars matches historical bars

Result: PASS (2026-10-14).
- Recording: `live ES.F NQ.F --minutes 1400` from 2026-10-13 10:25 ET (systemd user unit `fenrir-gate4`) to 2026-10-14 09:45 ET, across two sessions: the rest of Tuesday's and most of Wednesday's Globex. It printed `ticks=553575 (no volume: 0) lag median=0.29s p95=0.78s -> PASS` and saved `data/gate/ticks-2026-10-13T142537.jsonl`.
- `compare data/gate/ticks-2026-10-13T142537.jsonl --symbols ES.F NQ.F`, run 2026-10-14 09:47 ET, printed `compared=2557 mismatches=0` and `PASS`:

  | Symbol | Minutes compared | Mismatches beyond 1 tick | Minutes with equal volume | Volume, live = history |
  |---|---|---|---|---|
  | ES.F | 1,273 | 0 | 1,273/1,273 | 810,681 |
  | NQ.F | 1,284 | 0 | 1,284/1,284 | 338,526 |

  Both symbols are above the 1,242 minutes `compare` needs. The first and last recorded minute of each symbol are dropped, and only minutes that history has published count: 55 recorded minutes per symbol were not in history yet when `compare` ran.
- `compare` passes or fails on open, high, low and close only. It also prints the mismatches and a `volume` line for each symbol: how many minutes have equal live and historical volumes, and both totals. That line does not count towards the verdict.
- `volume` is the per-trade quantity: summing it per minute gives exactly the historical volume in every compared minute. That settles the reading inferred from the values in Item 3.
- `price == bid` (Item 3) does not move the bars: NQ, whose spread is often 0.5 or more, matched history within 1 tick in every minute. Whatever LSE's `price` is, its history is consistent with it to within 1 tick.

## Item 5 — LSE prices vs NinjaTrader sim fills

Result: not run yet (#99). Needs the add-on and a sim account: `docs/live-checklist.md` stage 2.
- Run: `uv run python scripts/gate_item5.py <sim account> --day <trading day> [<trading day> …]`
  from the repo root (`--db PATH` when this shell's `<private_dir>/fenrir.db` is not the journal
  the server wrote; `--allow-unmatched N`, below). It reads the account's journal read-only and
  calls no LSE: each `fill` row (the add-on's fill) against the journalled `bar` row (the LSE
  1-minute bar the runner traded on) of the same contract (`MNQ 12-26` → `NQZ26`) and the minute
  holding the fill time, both parsed with `datetime.fromisoformat` and keyed by the minute in UTC.
- PASS: at least 10 fills compared, none more than 1 tick outside its bar's [low, high] (item 4's
  tolerance; a sim market fill prints at the ask or bid while LSE's `price` may be the bid, Item
  3), and no unmatched fill (no bar of its contract for its minute), unless `--allow-unmatched N`
  accepts up to N. Each unmatched fill is listed with its seconds past the minute, and each one
  accepted is explained here. Qty-0 fills are skipped and do not count toward the 10.
- To record here: fills seen, compared, unmatched (and why, for each one accepted), worst distance
  in ticks, PASS/FAIL, the days, account, strategy and contract.

## Dry run on live data (#82)

Result: PASS (2026-10-14). SPEC Testing Strategy: a dry run on live LSE data comes before sim.
- **Setup:**
  - The server ran natively (`fenrir serve --port 8000`), with `FENRIR_PRIVATE_DIR` and `FENRIR_DATA_DIR` both set to scratch dirs, so the repo's `data/` was not touched.
  - No NinjaTrader add-on: `FENRIR_NT_TOKEN` was empty, and a dry run never calls the add-on. The status showed `nt.connected: false`.
  - Bar store and backtest, which arming requires: `fenrir data pull --series NQ --since 2025-06-01`, then `fenrir backtest strategies/orb.py --account lucid-flex-50k --contract MNQ` (89 attempts). Both together took 2 min 10 s.
- **Arming, through the API rather than by writing `fenrir.db`:**
  - At 09:53:15 ET, `POST /api/arming/arm` with `{"account":"dry82","strategy_path":"strategies/orb.py","rules_account":"lucid-flex-50k","contract":"MNQ","mode":"dry","phase":"evaluation"}` returned 200 with the armed row (`start_balance` and `peak_balance` null).
  - The socket had authenticated by 09:53:25, when the status showed `lse.connected: true`, with the account `warming`.
  - LSE replayed the day from 18:00 ET the day before. That replay and the warm-up were both done by 09:53:30: the status showed no conditions. The first `bar` row followed at 09:54:00.
- **Bars:**
  - Run 1 journalled 19 consecutive one-minute `bar` rows for `NQZ26`, with opens from 09:53 to 10:11 ET. None was missing or repeated, and each was journalled 0.16–0.36 s after its minute closed.
  - Run 2 (below) added 5 more, 10:13–10:17.
  - The 10:12 bar closed while the server was stopped. A restart's warm-up replays all of today's bars, the missed one included, with actions suppressed, and it journals no `bar` row for them. Run 2's same-day warm-up did not diverge from run 1's journal.
- **Stale-data pause and resume:**
  - With the default `stale_seconds` of 180 nothing paused: a bar came every minute.
  - Run 2 restarted the server with `FENRIR_STALE_SECONDS=45`. That setting is not pinned at arming, so the armed row stayed.
  - Each minute, 47 s after the last bar, the hub journalled an `alert`, `no NQ bar for 47s; new entries paused`, and the status showed `halted: true` with `halt_kind: "stale"`. This happened at 10:14:47, 10:15:47, 10:16:47 and 10:17:47 ET.
  - Each time, the next bar, 13 s later, lifted the pause: the status went back to `halt_kind: null` with no conditions.
  - No journal row marks a resume: the next `bar` row and the status show it.
- **Dry actions:** none were logged. ORB's 09:30–09:45 opening range (30497.75–30625.75, rebuilt from the item 4 recording) had already broken before the arm, and ORB trades once a day. The warm-up replays a breakout with its actions suppressed. Bars after the arm, such as 09:57, which closed at 30479.25 below the range, logged no action.
- **Disarm:** at 10:18:46 ET, `POST /api/arming/disarm` returned `{"ok":true,"error":null}` and journalled a `halt` row with reason `disarmed`. Disarming did not close the LSE socket; stopping the server (SIGINT, which ran the lifespan shutdown) did. Since #251 the hub closes it once no account is armed.

## Values downstream plans need

| Value | Used by | Answer |
|---|---|---|
| Default `fenrir data pull --since` (earliest usable day) | 03 | `2016-06-01` (first full month; data starts 2016-05-29 evening) |
| LSE symbol for our code `NQZ26` | 03, 04 | None per contract. History uses `NQ.F` / `ES.F` (continuous, unadjusted, LSE's own roll dates; see "Roll mismatch") |
| Timestamp shift to bar OPEN in UTC | 03 | none (timestamps are already bar open, UTC) |
| Roll-window days to skip | 03, 08, 21 | Every trading day from the `instruments.yaml` roll day (third Friday − 8 days, a Thursday) through the Tuesday of expiry week, inclusive |
| Live message → `Tick(ts, price, size, contract)` mapping | 04 | `tick.datetime.astimezone(UTC)` → `ts` (ticks with `replay=True` or no `datetime` are dropped), `price` → `price` (on the 0.25 tick in the 2026-10-13 run), `volume` (float, per-trade quantity as far as the values show; `None` or 0 = no trade, dropped) → `size`; `symbol` is `ES.F`/`NQ.F` and the message has no contract field, so the contract is our front month for that series outside the roll window; inside it `ES.F`/`NQ.F` still carry the expiring contract (see "Roll mismatch"; live takes no entries then, 21.3). `price` may be the bid (Item 3) |

## Decision

- Items 1–2: PASS → plan 03 may run. FAIL → stop; revisit the data source (Databento is the known alternative) before plan 03.
- Items 3–4: PASS → plans 04, 21 may run. FAIL → live trading blocked until the user picks another live source; plans 03, 05–18 continue.

Decision: Items 1–2 PASS → plan 03 may run, skipping the roll-window days (option 2 above). Items 3–4 PASS (item 3 on 2026-10-13 with the owner's key, after 2026-10-11's `registered` tier had streamed no futures; item 4 on 2026-10-14, 2,557 minutes with no mismatch), so plans 04 and 21 may use LSE live and DEC-7 (#18) is not needed. Item 5 waits on plan 22 (add-on and sim account).
