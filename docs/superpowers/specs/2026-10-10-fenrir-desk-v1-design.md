# Fenrir Desk v1 — Design

**Date:** 2026-10-10
**Status:** Draft for review

## 1. Purpose

Fenrir Desk is a local-only web app where a trader uses Claude Code to design, backtest and run futures strategies whose only goal is passing prop firm evaluations and reaching payouts on funded accounts. Claude writes deterministic strategy code; the app backtests it against the firm's rules and, once the user arms it, runs that same code live through NinjaTrader.

### Success criteria

- A strategy written by Claude can be backtested against any Lucid account type and produce a probability of passing and reaching a first payout, with expected net $ per attempt after fees.
- The user can compare many strategies against one account type, and one strategy across all account types, from the CLI or the UI.
- The user can arm a backtested strategy on a Lucid sim or live account and it trades through NinjaTrader with every position protected by a broker-side stop.
- Anyone can clone the repo and start the app with `docker compose up`.

### Decisions made during brainstorming

| Topic | Decision |
|---|---|
| Who decides trades | Claude writes strategy code; plain code trades it. No LLM in the live loop. |
| Prop firm | Lucid Trading only (all account types: Pro, Flex, Direct, Daily). |
| Instruments | ES, MES, NQ, MNQ. |
| Users | Single user per install; anyone can clone and run it. No accounts or login. |
| Strategies per account | One armed strategy per account. Different accounts can run different strategies. |
| Market data | London Strategic Edge (LSE) for both history and live bars. |
| Execution | NinjaTrader 8 (Windows) via a C# add-on: orders, accounts, positions only. |
| Stack | Vite + React + TypeScript + TanStack Router + TanStack Query; Python (FastAPI) backend and CLI; C# NinjaScript add-on. |
| Style | Functional core, imperative shell, everywhere. |
| Running | `docker compose up`. Development runs natively. |

### Out of scope for v1

- Take Profit Trader (its policy bans automated trading on Test, PRO and PRO+ accounts) and any other firm.
- Claude making live trade decisions, and a pre-session "Claude picks today's strategy" step.
- More than one strategy on the same account.
- Fallback to NinjaTrader as a market data source.
- Multi-user, hosting, authentication beyond the local session token.

## 2. Architecture

```
┌──────────── User machine (macOS or Windows) — Docker ─────────────┐
│  Browser ── React UI (static build, served by the Python server)  │
│     │  REST + WebSocket on 127.0.0.1 only, session token          │
│  Python server (FastAPI)  ── `fenrir` CLI shares the same core    │
│     ├─ Terminal   : PTY running a shell / `claude` in the repo    │
│     ├─ Data       : LSE history → data/bars/{ES,NQ}/1m/*.parquet  │
│     │               LSE live WebSocket → live bars                │
│     ├─ Rules      : rules/lucid/<type>-<size>.yaml                │
│     ├─ Backtester : fold(strategy, bars, rules) → report JSON     │
│     ├─ Strategies : strategies/*.py                               │
│     └─ Live runner: armed strategy → actions → NinjaTrader        │
└───────────────────────────────┬───────────────────────────────────┘
                                │ HTTP + WebSocket, shared token
                                │ (host.docker.internal or VM IP)
┌──────────── Windows (host or VM) ─────────────────────────────────┐
│  NinjaTrader 8 + Fenrir add-on (C#)                               │
│  orders · accounts · positions · fills · heartbeat · 16:40 flatten│
└───────────────────────────────────────────────────────────────────┘
```

### Functional core, imperative shell

All logic is pure functions over immutable data (frozen dataclasses, tuples). I/O — files, network, clock, orders, processes, the PTY — lives only in a thin shell that calls the core. Where a framework forces another style (NinjaScript subclasses, FastAPI handlers, process pools) that code stays thin and only forwards to the core. React uses function components and hooks; server state lives in TanStack Query.

### Storage

- `rules/`, `strategies/`, `instruments.yaml`: files in git.
- `data/`: Parquet bars, backtest result JSON, SQLite (trade journal, armed-strategy state). Gitignored.
- `.env`: LSE API key, NinjaTrader address and token. Gitignored; `.env.example` committed.

## 3. Market data

### History

`fenrir data pull` downloads 1-minute OHLCV bars for ES and NQ from LSE (`lse-data` Python client) into `data/bars/{ES,NQ}/1m/` as Parquet. MES and MNQ use the ES and NQ prices; only contract specs differ.

### Contracts and rollover

`instruments.yaml` holds contract specs and the rollover calendar:

| Contract | Data series | $ per point | Tick |
|---|---|---|---|
| ES | ES | 50 | 0.25 |
| MES | ES | 5 | 0.25 |
| NQ | NQ | 20 | 0.25 |
| MNQ | NQ | 2 | 0.25 |

The rollover calendar maps each date to the front-month contract code (e.g. `NQZ6`). Backtests and live trading use the same calendar. Prices used for signals and order levels are **unadjusted front-month** prices, never back-adjusted continuous prices.

### Live bars

The live runner subscribes to the LSE WebSocket and builds closed 1-minute bars for the current front-month ES and NQ contracts.

### Data gate (first task in the plan)

Before any live-trading work, verify with a real LSE key:

1. ES and NQ 1-minute history exists; record how far back it goes and any gaps.
2. Whether history is per-contract or continuous, and whether unadjusted front-month prices are available.
3. The live WebSocket streams real-time (not delayed) ES and NQ.
4. A day of LSE live bars matches LSE historical bars for the same minutes.
5. LSE prices match NinjaTrader sim fill prices within 1–2 ticks.

If 1–2 fail, the backtester is not trustworthy and the data source is revisited (Databento is the known alternative). If 3–5 fail, live trading is blocked until the user decides on another live source. The fetch step is the only code that knows about LSE, so swapping providers means rewriting it alone.

## 4. Rules library

One YAML file per Lucid account type and size, e.g. `rules/lucid/flex-50k.yaml` (values below are illustrative, not verified):

```yaml
firm: lucid
account: flex-50k
verified_on: 2026-10-10
source_url: https://...
automation_allowed: true
evaluation:
  profit_target: 3000
  max_drawdown: { amount: 2000, trailing: end_of_day }
  daily_loss_limit: null
  consistency: null            # or { max_day_pct: 40 }
  min_trading_days: 0
funded:
  max_drawdown: { amount: 2000, trailing: end_of_day }
  daily_loss_limit: null
  consistency: null
  payout: { max_pct_of_profit: 50, split_pct: 90, min_days: 0, min_profit: 0 }
session: { flat_by: "16:45", tz: America/New_York }
contracts: { max_minis: 4, micros_per_mini: 10 }
prohibited:
  microscalp: { max_profit_pct: 50, hold_seconds: 5 }
  hft: true
costs: { eval_fee: 0, activation_fee: 0, commission_per_side: { ES: 0, MES: 0, NQ: 0, MNQ: 0 } }
```

Values are entered from Lucid's own site, not third-party summaries. Lucid Direct has no evaluation, so its `evaluation` block is `null` and attempts start in the funded phase.

Each rule kind (profit target, trailing drawdown — end-of-day and intraday, daily loss limit, consistency, minimum days, payout eligibility, flat-by time, contract limits, microscalp) is implemented once as a pure function:

```python
def rule(state: AccountState, event: Event) -> AccountState | Breach: ...
```

An account type is the list of rule functions built from its YAML. Loading validates the YAML against a schema and rejects unknown keys.

## 5. Strategies

A strategy is one Python file in `strategies/` exporting:

```python
MARKET = "NQ"                       # price series; contract (NQ or MNQ) chosen per run
PARAMS = {"range_min": 15, "stop_ticks": 40, "target_ticks": 80}

def init(params: Params) -> State: ...

def step(state: State, bar: Bar, ctx: Ctx) -> tuple[State, list[Action]]: ...
```

- `ctx` carries recent bars, the current position, account state (drawdown remaining, today's P&L, progress to target, phase) and the bar's timestamp.
- `Action = Bracket(side, qty, stop, target) | Flatten() | Cancel(order_id)`. There is no entry without a stop.
- `step` must be pure: no I/O, no clock, no randomness. The same file runs in backtests and live.

## 6. Backtester

### Simulation

A backtest is one **(strategy, account type, contract)** run over the tuning period. It is a fold over 1-minute bars:

```
fold(step ∘ fills ∘ rules, bars, initial_state)
```

Fill model (always the conservative choice):

- Bracket entry fills at the next bar's open plus 1 tick slippage.
- If stop and target are both inside one bar, the stop fills first.
- Stops and market exits get 1 tick slippage; targets fill at the limit.
- Commissions from the rules file.
- Any trade that opens and closes in the same bar is flagged as a possible microscalp.

### Attempts

To estimate a probability rather than one lucky path, the backtester starts **one attempt on every trading day** in the period:

1. **Evaluation** until pass (target met with all rules satisfied) or breach (reason recorded).
2. **Funded**, for attempts that passed, until the first eligible payout or a breach.

Attempts are independent pure functions of their start date and run in parallel across CPU cores.

### Report (JSON, rendered by the UI)

- Pass rate; median and distribution of days to pass.
- Breakdown of failure reasons.
- Payout rate; median days to first payout; median first payout $.
- **P(pass and first payout)** and **expected net $ per attempt** after evaluation and activation fees.
- Microscalp flags.
- Results on the tuning period and the held-out period, side by side.
- A note that overlapping start dates mean the rate is an estimate, not an exact probability.

### Hold-out

The most recent stretch of data (default 12 months, configurable) is held out. Tuning runs (`fenrir backtest`) never see it. The full report (both periods) is produced by `fenrir evaluate` and by UI runs. A strategy whose held-out P(pass and payout) is far below its tuning value is flagged.

### Grids and cache

- CLI: `fenrir backtest strategies/*.py --account lucid-pro-50k`, `fenrir backtest strategies/orb.py --account 'lucid-*'`, `--contract MNQ`.
- UI: pick strategies × accounts × contracts and run as a background job with live progress. If the server restarts mid-run, unfinished pairs re-run next time.
- Results are cached by hash of strategy file + rules file + data version.
- The comparison table has two views: **by account** (rank strategies) and **by strategy** (compare accounts). Ranking sorts by tuning-period results only; held-out results are shown but never used to sort.

### Performance

Immutable state per bar across thousands of attempts may be slow in Python. An early plan task measures a full grid run. If it is too slow, indicators are precomputed in bulk or the inner loop is compiled; the functional design stays.

## 7. Claude Code workflow

### Built-in terminal

The UI's Workbench page has an xterm.js terminal connected over WebSocket to a PTY in the Python server (`pywinpty` on Windows, stdlib `pty` elsewhere; in Docker this is always Linux). It starts in the repo directory. Claude Code is installed in the image; its login is stored in a named Docker volume so the user signs in once.

Security requirements:

- The server listens on `127.0.0.1` only (Docker publishes `127.0.0.1:8000:8000`, never `8000:8000`).
- A random session token is generated at startup; the UI receives it from the served page and every REST and WebSocket request must carry it.
- WebSocket upgrades check the `Origin` header against the app's own origin.

### Project skill

`.claude/skills/fenrir-strategy/SKILL.md` teaches Claude the loop: read the target account's rules → inspect data (`fenrir data summary`, ad-hoc analysis) → write or edit a strategy → `fenrir backtest` → read the JSON report → iterate. It tells Claude to tune on the tuning period only.

### Arming is user-only

- No CLI command arms or disarms a strategy.
- The PTY's environment never contains the session token, so Claude in the terminal cannot call the arm endpoint.
- Arming requires a backtest of the current file hash against the account's type. If the file changed, the user must re-run the backtest.
- Arming a live (non-sim) account shows a confirmation with the backtest summary.

## 8. Live trading

### Modes

1. **Dry run**: live LSE bars, full pipeline, actions logged but not sent.
2. **Sim**: orders sent to a NinjaTrader sim account.
3. **Live**: orders sent to a Lucid account.

### Loop (per armed account, per closed bar)

1. Receive the closed LSE bar for the current front-month contract.
2. `step(state, bar, ctx)` → actions.
3. Apply rule functions.
4. **Pre-trade check**: reject any bracket whose stop-out would breach the daily loss limit, the drawdown limit or the contract limit. Log the rejection.
5. Send accepted actions to NinjaTrader with a unique client order ID.
6. Fills from NinjaTrader feed back as events.

Limits use the **stricter** of NinjaTrader's reported figures and the app's own computation.

### Reconciliation

On start and every reconnect, the runner compares its state with NinjaTrader's positions and working orders. Any mismatch halts that account, raises an alert, and blocks trading until the user resolves it. NinjaTrader is the source of truth for positions.

### Failure handling

| Situation | Behavior |
|---|---|
| App, container, Mac or VM link lost | No new entries. Open positions keep their NinjaTrader-side stop and target. |
| Heartbeat lost with an open position | Keep the bracket (default). Configurable to flatten. |
| No LSE bar for N seconds in session, or LSE prices drift from NinjaTrader fills beyond a threshold | Pause new entries and alert. |
| 16:40 America/New_York | The add-on flattens and cancels on its own, whether or not Python is connected. |
| Account reaches safety margin (default 80% of daily loss limit used) | Halt that account for the rest of the day. |
| Kill switch (UI) | Flatten all, cancel all, disarm all accounts. |

### Trade journal

SQLite records every bar decision, action, rejection, fill and alert. The UI shows it; Claude can read it to compare live trades with backtests.

## 9. NinjaTrader add-on (C#)

A NinjaScript add-on that hosts an HTTP + WebSocket server on a configurable port. Every request carries the shared token from `.env`.

- **Requests**: list accounts, positions and working orders; submit bracket (entry + OCO stop and target) with client order ID; flatten; cancel.
- **Stream**: account balance and P&L, positions, order updates, fills, heartbeat.
- **Deduplication**: a client order ID already seen is ignored, so retries never double-order.
- **Independent safety**: flatten and cancel all at 16:40 America/New_York.
- **No market data and no trading decisions.**
- Kept as thin as NinjaScript allows; it forwards to NinjaTrader's API and nothing else.

Distributed as an importable NinjaScript zip, built from source in the repo.

## 10. User interface

| Page | Contents |
|---|---|
| Workbench | Terminal (Claude Code) on the left; latest backtest report on the right, refreshing when a backtest finishes. |
| Rules | Lucid account types and sizes; validated edit form that writes the YAML; warning when `verified_on` is over 30 days old. |
| Data | Downloaded coverage and gaps per series; Pull button; rollover table. |
| Backtests | Run form (strategies × accounts × contracts); job progress; comparison table (by account / by strategy); report page with days-to-pass distribution, failure reasons and sample equity curves. |
| Strategies | Strategy files, params, latest backtests; Arm/Disarm per account with confirmation. |
| Live | LSE and NinjaTrader connection status; per account: balance, distance to every limit, positions, today's journal; kill switch. |
| Settings | NinjaTrader address and token, LSE key, hold-out length, safety margin, stale-data threshold. |

Charts use TradingView Lightweight Charts.

## 11. Running and installing

### Run (users)

```
git clone <repo> && cd fenrir-desk
cp .env.example .env      # add LSE key, NinjaTrader address and token
docker compose up
```

Open `http://localhost:8000`.

- Dockerfile is multi-stage: Node builds the UI; the Python runtime stage (with `uv`) holds the server, CLI, built UI, Claude Code and `git`.
- `restart: unless-stopped`.
- Bind mounts: `strategies/`, `rules/`, `instruments.yaml`, `data/`, `.env`. Named volume: Claude Code login.
- NinjaTrader address: `host.docker.internal:<port>` when NinjaTrader runs on the same Windows host; the VM's IP for macOS + VM.

Live trading additionally requires NinjaTrader 8 on Windows with the add-on imported. The README has a guide for importing it, finding the VM IP, and opening the port in Windows Firewall.

### Develop

`uv run fenrir serve` and `npm run dev` natively, for hot reload.

## 12. Testing

- **Rules**: a unit test per rule function with hand-built scenarios (trailing drawdown locking, consistency broken by one big day, flat-by time, contract limits with micros).
- **Backtester**: golden tests on tiny synthetic bar sets with hand-computed results; Hypothesis property tests (no trades after a breach, fills never better than the bar allows, same input → same report).
- **Bridge**: a fake add-on in Python to test reconnects, duplicate order IDs, reconciliation mismatches, dropped connections and stale data.
- **Add-on**: manual checklist on a NinjaTrader Sim account (bracket, flatten, cancel, dedupe, 16:40 flatten, token rejection).
- **Live runner**: dry-run mode against live LSE data before sim; sim before live.
- **Security**: tests that the server rejects requests without the token and WebSocket upgrades from foreign origins.
- **Frontend**: TypeScript typecheck; Playwright flows for running a backtest, arming needing confirmation, and the kill switch.

## 13. Risks

| Risk | Mitigation |
|---|---|
| LSE lacks real-time or deep CME futures data | Data gate is the first plan task; fetch step is the only LSE-specific code. |
| Lucid rules change or third-party summaries are wrong | Rules entered from Lucid's site; `verified_on` with staleness warning. |
| Overfitting from tuning many strategies on the same data | Hold-out period; ranking never sorts by hold-out; gap flag. |
| Backtest fills more optimistic than live | Conservative fill model; dry run and sim before live; journal for live vs backtest comparison. |
| Python fold too slow | Early performance measurement; precompute or compile if needed. |
| Browser terminal as an attack surface | Loopback-only binding, session token, Origin check, `127.0.0.1` port publishing. |
| App or host goes down with a position open | Broker-side brackets; add-on flattens at 16:40 on its own. |
