# Spec: Fenrir Desk v1

**Status:** Draft for review · **Updated:** 2026-10-11 · Replaces the 2026-10-10 v1 design spec

Cross-plan contracts (exact names, signatures, file paths, JSON shapes) live in `docs/contracts.md`. This spec says what and why; the contracts say exactly how modules meet. When they disagree, stop and ask.

## Objective

Fenrir Desk is a local-only web app where one futures prop trader uses Claude Code to design, backtest and run strategies whose only goal is passing Lucid Trading evaluations and reaching payouts on funded accounts. Claude writes deterministic strategy code; the app backtests it against the firm's rules and, once the user arms it, runs that same code live through NinjaTrader.

**User:** one trader per install, who knows prop firm rules, futures and NinjaTrader (see `PRODUCT.md`). Two scenes: the Live page glanced at on a second screen during market hours, and research in the evening with Claude in the Workbench terminal.

**Optimizes for** time-to-pass and time-to-payout under one firm's rules, not raw profit.

### Decisions

| Topic | Decision |
|---|---|
| Who decides trades | Claude writes strategy code; plain code trades it. No LLM in the live loop. |
| Prop firm | Lucid Trading only: Pro, Flex, Direct, Daily. Flex is entered first. |
| Instruments | ES, MES, NQ, MNQ. Micros use the mini's price series. |
| Users | Single user per install; anyone can clone and run it. No accounts or login. |
| Strategies per account | One armed strategy per account. Different accounts can run different strategies. |
| Market data | London Strategic Edge (LSE) for history. Live bars from LSE are conditional on data gate item 3; if LSE cannot stream real-time futures, the live source is reopened (see Open Questions). Only `fenrir.data.lse` knows LSE. |
| Execution | NinjaTrader 8 (Windows) via a C# add-on: orders, accounts, positions only. |
| Style | Functional core, imperative shell, everywhere. |
| Running | `docker compose up`. Development runs natively. |
| Evaluation price | A user input per account (`eval_price` in `data/accounts.yaml`), not a rule: it varies with coupons. |
| Daily loss limit | Per-account user choice (`dll_on`). When on, hitting it halts the day (Lucid soft breach); the attempt continues. |
| Roll window | Thursday 8 days before expiry through Tuesday of expiry week: no bars in backtests, no entries live (LSE rolls later than our calendar). |

### Out of scope for v1

- Take Profit Trader (bans automated trading on Test, PRO and PRO+) and every other firm.
- Claude making live trade decisions, or a pre-session "Claude picks today's strategy" step.
- More than one strategy on the same account.
- NinjaTrader as a market data source.
- Multi-user, hosting, authentication beyond the local session token.

## Tech Stack

- **Python 3.12**, `uv`, src layout, package `fenrir`, console script `fenrir = "fenrir.cli.main:app"`.
- **Python libraries:** `typer`, `pydantic>=2` (frozen, `extra="forbid"`), `pyyaml`, `polars` + `pyarrow` (Parquet), `pandas-market-calendars` (CME_Equity), `lse-data>=0.14` (import name `lse`). Later: `fastapi` + `uvicorn[standard]`, `httpx` + `websockets`. Dev: `pytest`, `pytest-asyncio`, `hypothesis`, `ruff`.
- **Web (plan 12+):** Node 22, Vite, React 19, TypeScript strict, Tailwind CSS v4, shadcn/ui (Radix, New York), TanStack Router (code-based routes) + TanStack Query, `@xterm/xterm`, `lightweight-charts` (all charts), self-hosted `@fontsource` Chakra Petch + IBM Plex Mono, Playwright. Visual system: `DESIGN.md`.
- **Add-on:** NinjaScript (C#, .NET Framework 4.8) inside NinjaTrader 8.
- **Storage:** files in git (`rules/`, `strategies/`, `instruments.yaml`); gitignored `data/` (Parquet, results JSON, jobs, SQLite, `accounts.yaml`); `.env` for secrets.

## Commands

Available now:

```
Install:     uv sync
Test:        uv run pytest -q
Lint:        uv run ruff check . && uv run ruff format --check .
Format:      uv run ruff format .
Pull data:   uv run fenrir data pull [--series ES --series NQ] [--since YYYY-MM-DD]
Data info:   uv run fenrir data summary --json
Data gate:   uv run --with lse-data python scripts/lse_gate.py <command>
Backtest:    uv run fenrir backtest <strategy globs...> --account <glob> [--account ...] [--contract MNQ ...] [--json]   # tuning only, cached
Evaluate:    uv run fenrir evaluate <same args>                                                                      # tuning + hold-out
```

Planned (the plan that adds each one adds it here):

```
Serve:       uv run fenrir serve [--port 8000] [--dev] [--static-dir web/dist]                    # (11)
Web dev:     npm --prefix web run dev                                                             # (12)
Typecheck:   npm --prefix web run typecheck                                                       # (12)
E2E:         npx --prefix web playwright test                                                     # (12)
Run app:     docker compose up        → http://localhost:8000                                     # (18)
```

## Project Structure

```
instruments.yaml         → contract specs and rollover calendar (2008–2030)
rules/lucid/             → one YAML per Lucid account type and size, entered from Lucid's site
strategies/              → strategy files written by Claude (contract in strategies/CLAUDE.md)
src/fenrir/core/         → pure shared types: Bar, actions, contract specs, rollover, trading days
src/fenrir/data/         → LSE pull, Parquet store, front-month loading, live bars
src/fenrir/rules/        → schema, loader, account state, rule functions, pre-trade check
src/fenrir/strategy/     → Ctx, Position, Strategy, loader
src/fenrir/backtest/     → fill model, attempt fold, attempts, report, grid, cache
src/fenrir/cli/          → Typer CLI, one module per command group; thin shell
src/fenrir/server/       → FastAPI app, auth, routes, PTY, jobs          (planned, 11)
src/fenrir/bridge/       → add-on client, reconciliation                 (planned, 20)
src/fenrir/live/         → live runner, journal, safety, arming          (planned, 21)
web/                     → Vite React app                                (planned, 12)
addon/                   → C# NinjaScript add-on                         (planned, 19)
.claude/skills/fenrir-strategy/ → project skill for the strategy loop    (planned, 17)
scripts/                 → one-off scripts run by hand; never imported
tests/                   → pytest, mirrors src/fenrir/; fixtures/ for shared data
design/                  → static prototypes; DESIGN.md records the system
docs/                    → data-gate findings, cross-plan contracts, Lucid rule mechanics
data/                    → gitignored runtime data
```

Every directory with its own job has a short `CLAUDE.md` and a row in root `CLAUDE.md` → Directory Map.

## Capability Map

| Module id | Responsibility | Depends on | Plans | Status |
|---|---|---|---|---|
| `core` | Types, instruments, rollover, sessions, settings, CLI root | — | 01 | Done |
| `data` | LSE gate, history pull, store, live bars | core | 02, 03, 04 | Gate 1–2 passed, 3–5 open; 03 done; 04.1 bar builder done |
| `rules` | Schema, loader, state, rule functions, pre-trade | core | 05, 06 | Done (Flex files only) |
| `strategy` | Strategy contract and loader | rules, core | 07 | Done |
| `backtest` | Fill model, attempt fold, attempts, report, grid, cache | data, rules, strategy | 08, 09, 10 | 08, 09 done; 10 not started |
| `server` | FastAPI shell, auth, PTY, jobs/rules/data APIs | backtest, data, rules | 11, 13–16 | 14.1 job state done |
| `web` | React UI, every page | server | 12–16, 23, 24 | Not started |
| `skill` | Claude project skill | backtest | 17 | Not started |
| `docker` | Image and compose | server, web | 18 | Not started |
| `addon` | NinjaTrader C# add-on | — | 19 | Not started |
| `bridge` | Python add-on client, reconciliation | addon (contract only), core | 20 | 20.1 instrument names done |
| `live` | Runner, journal, safety, orders, arming, kill switch | data, strategy, backtest, server, bridge | 21–24 | Not started |

Build order: core → data, rules → strategy → backtest → server → web, skill → docker. In parallel: addon → bridge. Then live (needs both chains). A plan may start once every plan it needs is merged.

## Design

### Architecture

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

### Market data

- **History:** `fenrir data pull` pages LSE's continuous `ES.F`/`NQ.F` 1-minute bars (bar-open time, UTC, unadjusted front month; from 2016-05-30) into `data/bars/{ES,NQ}/1m/<contract>.parquet` plus `manifest.json`. `data_version` is the manifest's sha256. A full-series load is about 13 s and 2.2 GB, so each process loads a series once per `data_version`.
- **Contracts:** `instruments.yaml` holds specs (ES $50/pt, MES $5, NQ $20, MNQ $2; tick 0.25) and the rollover calendar (Thursday 8 days before quarterly expiry). Contract codes use a two-digit year (`NQZ26`). Signals and orders use unadjusted front-month prices, never back-adjusted.
- **Roll window:** see Decisions. `load_bars` returns no bars on those days; coverage counts neither data nor gaps there.
- **Live bars:** a pure tick → closed 1-minute bar builder fed by the LSE WebSocket.
- **Data gate:** findings in `docs/data-gate.md`. If items 1–2 had failed, the backtester would not be trustworthy (Databento is the known alternative). If 3–5 fail, live trading is blocked until the user picks another live source.

### Rules

One YAML per Lucid account type and size (`rules/lucid/flex-50k.yaml`), validated by a strict pydantic schema that rejects unknown and duplicate keys. Values come from Lucid's own site, with `verified_on` (null = unverified, shown stale) and `source_url`. Lucid Direct has no evaluation: `evaluation: null`, attempts start funded.

Fields: `evaluation` (profit target, max drawdown, daily loss limit, consistency, min days), `funded` (same plus payout and optional scaling tiers), `max_drawdown.trailing` (`end_of_day` | `intraday`) and `locks_at`, `session.flat_by`, `contracts` (max minis, micros per mini), `prohibited` (microscalp, HFT), `costs` (activation fee, commission per side). No `eval_fee`.

Each rule kind is one pure function `(AccountState, Event) -> AccountState | Breach`. Only two things breach: `max_drawdown`, and `flat_by` (an engine failed to flatten by 16:40, which is an engine bug). The daily loss limit halts the day; consistency blocks pass and payout; contract limits are pre-trade rejections; microscalp is a flag.

### Strategies

One Python file in `strategies/` exporting `MARKET`, `PARAMS`, `init(params) -> State` and `step(state, bar, ctx) -> (State, list[Action])`. `ctx` holds the last ≤500 bars, the position, an account view (drawdown remaining, daily loss remaining, today's P&L, target remaining, phase) and the contract spec. `Action = Bracket(side, qty, stop, target) | Flatten() | Cancel(order_id)`: there is no entry without a stop, and a new bracket is rejected while a position is open. `step` is pure; the loader rejects I/O imports. The same file runs in backtests and live.

### Backtester

- **Fold:** one (strategy, account, contract) run is a fold over 1-minute bars.
- **Fill model (always conservative):** entry at next bar's open + 1 tick; stop first when stop and target share a bar; stops and market exits get 1 tick slippage, targets fill at the limit; commissions from the rules file; same-bar open-and-close is flagged as possible microscalp.
- **Attempts:** one attempt starts on every trading day: evaluation until pass or breach, then funded until first eligible payout or breach. Attempts are independent and run in parallel.
- **Report JSON:** pass rate and days-to-pass distribution, failure reasons (`max_drawdown`, `flat_by`, `no_result`), payout rate, median days and $ to first payout, P(pass and payout), `expected_gross_per_attempt` (fee-free), microscalp flags, up to 6 sample equity curves, tuning and hold-out side by side, and a note that overlapping starts make rates estimates. Net per attempt = gross − `eval_price`, computed on read, never cached.
- **Hold-out:** the most recent 12 months (configurable). `fenrir backtest` never sees it; `fenrir evaluate` and UI runs show both. Held-out P(pass and payout) under 50% of tuning sets `holdout_gap`.
- **Grids and cache:** strategy globs × account globs × contracts. Cached by sha256 of strategy file, rules file, data version, contract, hold-out settings and `dll_on`. Comparison by account or by strategy ranks on tuning results only.

### Claude Code workflow

- **Terminal:** xterm.js on the Workbench page over WebSocket to a PTY in the server, started in the repo. Claude Code is installed in the image; its login is kept in a named volume.
- **Security:** binds `127.0.0.1` (Docker publishes `127.0.0.1:8000:8000`); Host header checked; random session token per start, injected into `index.html`, required on every REST (`X-Fenrir-Token`) and WebSocket (`?token=`) request; WebSocket `Origin` checked.
- **Project skill:** `.claude/skills/fenrir-strategy/SKILL.md` teaches read rules → inspect data → write strategy → `fenrir backtest` → read report → iterate, on the tuning period only.
- **Arming is user-only:** no CLI command arms; the PTY environment drops the token and every `FENRIR_*` var; arming needs a backtest of the current file hash against the account type, contract and current `dll_on`; arming a live account needs a confirmation showing the backtest summary.

### Live trading

- **Modes:** dry run (actions logged, not sent) → sim → live.
- **Loop per armed account, per closed bar:** bar → `step` → rule functions → pre-trade check (reject any bracket whose stop-out would breach drawdown or daily loss, or exceed the contract limit) → send with a unique client order ID → fills feed back as events. Limits use the stricter of NinjaTrader's figures and the app's own.
- **Reconciliation:** on start and every reconnect. Any mismatch halts the account until the user resolves it. NinjaTrader is the source of truth for positions.
- **Failure handling:**

| Situation | Behavior |
|---|---|
| App, container or link lost | No new entries; open positions keep broker-side stop and target. |
| Heartbeat lost with a position | Keep the bracket (default) or flatten (setting). |
| No LSE bar for N seconds in session, or LSE drifts from fills | Pause new entries and alert. |
| 16:40 America/New_York | Add-on flattens and cancels on its own, Python connected or not. |
| Safety margin hit (default 80% of daily loss limit) | Halt the account for the day. |
| Strategy file hash changed while armed | Halt the account. |
| Kill switch | Flatten all, cancel all, disarm all. |

- **Journal:** SQLite `data/fenrir.db` records every bar decision, action, rejection, fill, alert and halt.

### NinjaTrader add-on

HTTP + WebSocket server inside NinjaTrader on a configurable port, shared token on every request. Lists accounts, positions and working orders; submits brackets (market entry + OCO stop/target); flatten, cancel, flatten-all; streams account, position, order, fill and a 2 s heartbeat. Duplicate client order IDs are ignored. Flattens and cancels at 16:40 ET by itself. No market data, no decisions; forwards to NinjaTrader's API only. Shipped as an importable NinjaScript zip built from the repo.

### User interface

| Page | Contents |
|---|---|
| Workbench | Terminal (Claude Code) left; latest backtest report right, refreshing when a backtest finishes. |
| Rules | Account types and sizes; field-by-field form writing the YAML; warning when `verified_on` is null or over 30 days old. |
| Data | Coverage and gaps per series; Pull button; rollover table. |
| Backtests | Run form (strategies × accounts × contracts); job progress; comparison by account / by strategy; report page. |
| Strategies | Files, params, latest backtests; Arm/Disarm per account with confirmation. |
| Live | Feed, broker and drift status; per account: balance, distance to every limit, position, today's journal; kill switch. |
| Settings | NinjaTrader address and token, LSE key, hold-out length, safety margin, stale threshold; per-account `eval_price` and `dll_on`. |

Follows `PRODUCT.md` and `DESIGN.md` exactly: dark only, radius 0, no shadows, colour only for abnormal or selected state, never green.

### Running

Users: `git clone`, `cp .env.example .env` (LSE key, NinjaTrader address and token), `docker compose up`, open `http://localhost:8000`. Multi-stage image (Node builds UI; Python runtime with `uv`, built UI, Claude Code, `git`), `restart: unless-stopped`, bind mounts for `strategies/`, `rules/`, `instruments.yaml`, `data/`, `.env`. NinjaTrader at `host.docker.internal:<port>` on the same Windows host, or the VM's IP from macOS. Live trading also needs NinjaTrader 8 with the add-on imported; the README covers import, VM IP and Windows Firewall.

## Code Style

Functional core, imperative shell. Core = pure functions over `@dataclass(frozen=True, slots=True)` values and tuples: no I/O, clock, randomness or mutation. Shell = thin functions that read files, network or clock and call the core. Framework-forced code (FastAPI handlers, NinjaScript, process pools) only forwards. React: function components and hooks; server state only in TanStack Query.

```python
"""Daily loss limit: today's realized + worst open P&L at or below -limit halts the day.

Lucid treats this as a soft breach, so it halts rather than breaches.
"""

from dataclasses import replace

from fenrir.rules.state import AccountState, Event, Mark, Rule, at_or_below


def daily_loss_limit(limit: float) -> Rule:
    def rule(state: AccountState, event: Event) -> AccountState:
        today = state.balance - state.day_start_balance
        if isinstance(event, Mark) and at_or_below(today + event.worst_open_pnl, -limit):
            return replace(state, halted_today=True)  # ledger.book clears it at DayClosed
        return state

    return rule
```

- Ruff: line length 100, rules `E F I UP B`, target py312.
- Module docstring says what the file does; comments only for the why.
- Money in `float` dollars; times tz-aware UTC; trading days as `date`.
- Commits: Conventional Commits, scope = module id (`core`, `data`, `rules`, `strategy`, `backtest`, `server`, `web`, `terminal`, `bridge`, `live`, `addon`, `docker`, `skill`).

## Testing Strategy

- **Framework:** pytest (`--import-mode=importlib`) + Hypothesis; `tests/` mirrors `src/fenrir/` (`tests/rules/test_drawdown.py` ↔ `src/fenrir/rules/drawdown.py`). Shared data in `tests/fixtures/`.
- **Rules:** a unit test per rule function with hand-built scenarios (trailing drawdown locking, consistency broken by one big day, flat-by, contract limits with micros).
- **Backtester:** golden tests on tiny synthetic bars with hand-computed results; property tests (no trades after a breach, fills never better than the bar allows, same input → same report).
- **Data:** LSE is never called in tests; the pull is tested with an injected fake fetch, the LSE shell with a monkeypatched client.
- **Bridge:** a fake add-on in Python for reconnects, duplicate order IDs, reconciliation mismatches, dropped connections and stale data.
- **Add-on:** manual checklist on a NinjaTrader Sim account (bracket, flatten, cancel, dedupe, 16:40 flatten, token rejection).
- **Live runner:** dry run on live LSE data before sim; sim before live.
- **Security:** server rejects requests without the token, foreign Hosts and foreign WebSocket Origins.
- **Frontend:** TypeScript typecheck; Playwright for running a backtest, arming needing confirmation, and the kill switch.
- **Coverage:** no percentage target; every rule kind, fill case and halt path has a test that fails without it.

## Boundaries

**Always**
- Run `uv run pytest -q` and both ruff checks before every commit.
- Read a directory's `CLAUDE.md` before working in it; new directory → its `CLAUDE.md` + a Directory Map row in the same commit.
- Keep the core pure; I/O only in the shell.
- Implement cross-plan contracts exactly; a contract change updates `docs/contracts.md` in the same commit and says so in the body.
- Enter rule values from Lucid's own site with `verified_on` and `source_url`.
- Pick the conservative choice in any fill or limit ambiguity.

**Ask first**
- Adding or upgrading a dependency.
- Changing a rules YAML value, `instruments.yaml`, the roll window or the fill model.
- Changing the report schema or the cache key.
- Anything touching order routing, pre-trade limits, the safety margin, reconciliation or the 16:40 flatten.
- When code and this spec disagree.

**Never**
- Commit `.env`, API keys, broker credentials or anything under `data/`.
- Add a CLI path, API path or environment leak that lets Claude arm, disarm or reach the session token.
- Tune on the hold-out period or rank by it.
- Use back-adjusted prices, or allow an entry without a stop.
- Put I/O, clock or randomness in a strategy `step` or a core function.
- Bind the server to `0.0.0.0` outside the Docker image.
- Skip, delete or weaken a failing test to get to green.
- Send orders to a live (non-sim) account from tests or scripts.

## Success Criteria

1. `fenrir evaluate strategies/orb.py --account 'lucid-*' --json` returns, for every Lucid account file and both contracts of the strategy's market, a report with pass rate, P(pass and payout) and expected gross per attempt for tuning and hold-out; the API adds net per attempt from `eval_price`.
2. The same inputs (strategy file, rules file, data version, settings) give a byte-identical report, served from cache on the second run.
3. The comparison views rank many strategies on one account, and one strategy across accounts, by tuning results only, from the CLI and the UI.
4. Arming is refused (409) without a backtest matching the file hash, account, contract and `dll_on`; arming a live account needs confirmation.
5. In dry run, sim and live, every bracket sent has a stop; a bracket whose stop-out would breach a limit is rejected and journalled.
6. With Python disconnected, the add-on flattens and cancels all at 16:40 ET on a Sim account (manual checklist).
7. The server returns 401 without the token, 400 for a foreign Host, and closes foreign-Origin WebSockets with 4401.
8. From a clean clone, `cp .env.example .env && docker compose up` serves the app at `http://localhost:8000`.

## Risks

| Risk | Mitigation |
|---|---|
| LSE has no real-time CME futures on the current tier | Gate item 3 before plan 04; fetch is the only LSE-aware code. |
| Lucid rules change or summaries are wrong | Rules from Lucid's site; `verified_on` staleness warning. |
| Overfitting from many strategies on the same data | Hold-out; ranking never uses it; `holdout_gap` flag. |
| Backtest fills better than live | Conservative fill model; dry run and sim first; journal for live vs backtest. |
| Python fold too slow | Plan 09 measures a full grid; precompute indicators or compile the inner loop; design stays functional. |
| Browser terminal as attack surface | Loopback binding, Host check, token, Origin check. |
| Host goes down with a position open | Broker-side brackets; add-on flattens at 16:40 on its own. |

## Open Questions

1. **Live futures on LSE (gate item 3).** On 2026-10-10 the socket reported tier `registered` with no `ES.F`/`NQ.F` among its streamable symbols. If futures ticks don't arrive in market hours, what's the live source: an LSE tier upgrade, Databento, or something else? This blocks plans 04, 21 and 22.
2. **Gate items 4–5** (live vs historical bars; LSE vs NinjaTrader fills) have not been run.
3. **ES 2023-03 roll** is ambiguous in the data; the fixed roll window covers it, but it isn't confirmed.
4. **Pro, Direct and Daily rules** are not entered yet, and `automation_allowed` has not been checked for them on Lucid's site.
5. **Fold performance** is unmeasured until plan 09's grid run.
