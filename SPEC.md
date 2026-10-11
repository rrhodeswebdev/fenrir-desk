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
| Terminal isolation (DEC-1) | In the Docker image the Workbench terminal and every strategy load or `step` run as an unprivileged `claude` user; the server runs as root from the image, keeps secrets, `fenrir.db` and the session key in a root-only volume, and never imports or executes a file the terminal can write. The session reaches the browser as an HttpOnly cookie via a one-time launch link, never in a page. Running natively, the terminal runs as you and is not isolated. |
| Rejected WebSockets (DEC-2) | Refused at the handshake (HTTP 403), never accepted then closed. |
| Rules files from the UI (DEC-3) | Written with a `ruamel.yaml` round-trip: comments, number formatting and layout are kept; only changed values move. |
| Docker mounts (DEC-6) | The whole repo is mounted at `/app` so Claude can read the code and commit; secrets never live in it (see Running). |

### Out of scope for v1

- Take Profit Trader (bans automated trading on Test, PRO and PRO+) and every other firm.
- Claude making live trade decisions, or a pre-session "Claude picks today's strategy" step.
- More than one strategy on the same account.
- NinjaTrader as a market data source.
- Multi-user, hosting, authentication beyond the local session cookie.

## Tech Stack

- **Python 3.12**, `uv`, src layout, package `fenrir`, console script `fenrir = "fenrir.cli.main:app"`.
- **Python libraries:** `typer`, `pydantic>=2` (frozen, `extra="forbid"`), `pyyaml`, `polars` + `pyarrow` (Parquet), `pandas-market-calendars` (CME_Equity), `lse-data>=0.14` (import name `lse`). Later: `fastapi` + `uvicorn[standard]`, `httpx` + `websockets`, `ruamel.yaml` (rules round-trip), `pywinpty` (Windows PTY only). Dev: `pytest`, `pytest-asyncio`, `hypothesis`, `ruff`.
- **Web (plan 12+):** Node 22, Vite, React 19, TypeScript strict, Tailwind CSS v4, shadcn/ui (Radix, New York), TanStack Router (code-based routes) + TanStack Query, `@xterm/xterm`, `lightweight-charts` (all charts), self-hosted `@fontsource` Chakra Petch + IBM Plex Mono, Playwright. Visual system: `DESIGN.md`.
- **Add-on:** NinjaScript (C#, .NET Framework 4.8) inside NinjaTrader 8.
- **Storage:** files in git (`rules/`, `strategies/`, `instruments.yaml`); gitignored `data/` (Parquet, results JSON, jobs, `accounts.yaml`); a private directory (`FENRIR_PRIVATE_DIR`, default `data/private`; a root-only volume in Docker) for `fenrir.db`, the session key and, in Docker, the secrets file; `.env` for secrets when running natively.

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
Backtest:    uv run fenrir backtest '<strategy glob>'... --account '<glob>' [--account ...] [--contract MNQ ...] [--json]   # tuning only, cached; quote globs (zsh)
Evaluate:    uv run fenrir evaluate <same args>                                                                      # tuning + hold-out
Compare:     uv run fenrir compare --view account|strategy [--json]                                                  # latest cached reports, ranked by tuning
Web install: npm --prefix web ci
Web dev:     npm --prefix web run dev            # proxies /api, /ws, /launch to :8000
Typecheck:   npm --prefix web run typecheck
Web test:    npm --prefix web test -- --run      # Vitest unit tests
Web build:   npm --prefix web run build          # writes web/dist/
```

Planned (the plan that adds each one adds it here):

```
Serve:       uv run fenrir serve [--port 8000] [--dev] [--static-dir web/dist]                    # (11)
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
web/                     → Vite React app (scaffold and API client; pages from 12.2)
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
| `backtest` | Fill model, attempt fold, attempts, report, grid, cache | data, rules, strategy | 08, 09, 10 | 08, 09, 10.1–10.3, 10.5 done; 10.4 not started |
| `server` | FastAPI shell, auth, PTY, jobs/rules/data APIs | backtest, data, rules | 11, 13–16 | 14.1 job state done |
| `web` | React UI, every page | server | 12–16, 23, 24 | 12.1 scaffold and API client done |
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
│     │  REST + WebSocket on 127.0.0.1 only, session cookie         │
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

One YAML per Lucid account type and size (`rules/lucid/flex-50k.yaml`), validated by a strict pydantic schema that rejects unknown and duplicate keys. Values come from Lucid's own site, with `verified_on` (null = unverified, shown stale) and `source_url`. Lucid Direct has no evaluation: `evaluation: null`, attempts start funded. The Rules page saves through a `ruamel.yaml` round-trip, so provenance comments and formatting survive an edit.

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
- **Security:** binds `127.0.0.1` (Docker publishes `127.0.0.1:8000:8000`); Host header checked. A random session key is kept in the private directory. `fenrir serve` prints a one-time launch link (`/launch?code=…`, a fresh single-use code per start); opening it sets an HttpOnly, SameSite=Strict session cookie that every REST and WebSocket request needs. No page, header, query string or API returns the session key. Every non-GET `/api` request whose `Origin` is missing or not the app's own, or whose `Sec-Fetch-Site` is present and not `same-origin`, gets 403 (SameSite does not separate localhost ports). WebSocket `Origin` checked; a bad cookie or Origin is refused at the handshake (HTTP 403). `127.0.0.1` redirects to `localhost` so the cookie has one host. A lost session is recovered by restarting the server and opening its new launch link; deleting `session.key` signs every browser out.
- **Terminal isolation (Docker):** the PTY runs as the unprivileged `claude` user, with every `FENRIR_*` var dropped. It can read and write the repo, `data/bars`, `data/results` and `data/jobs`, and run `uv run fenrir backtest`; it cannot read the private volume (secrets, `fenrir.db`, session key), the server's environment or its logs. Nothing it can write runs as root: the server runs its own code and UI from the image (`/opt/fenrir`, outside `/app`), and every strategy load or `step` (the strategy list, backtest jobs, the live runner) runs in a child process as `claude` with no secrets in its environment, talking to the server over a pipe. So the terminal cannot arm, reach the session or reach NinjaTrader. Natively (`uv run fenrir serve`) the terminal runs as you and can read what you can; `fenrir serve` says so at start.
- **Project skill:** `.claude/skills/fenrir-strategy/SKILL.md` teaches read rules → inspect data → write strategy → `fenrir backtest` → read report → iterate, on the tuning period only.
- **Arming is user-only:** no CLI command arms; the PTY cannot reach the session or the `armed` table (see Terminal isolation); arming needs a backtest of the current strategy and rules file hashes against the account type, contract and current `dll_on`; arming a live account needs a confirmation showing the backtest summary, `dll_on`, the rules hash and the computed liquidation level. The backtest match is process evidence, not a security control: reports and `accounts.yaml` are files the terminal can write. The user's confirmation is the control.

### Live trading

- **Modes:** dry run (actions logged, not sent) → sim → live.
- **Loop per armed account, per closed bar:** bar → `step` → rule functions → pre-trade check (reject any bracket whose stop-out would breach drawdown or daily loss, or exceed the contract limit) → send with a unique client order ID → fills feed back as events. Limits use the stricter of NinjaTrader's figures and the app's own.
- **Starting state (DEC-4a):** sim and live arming require the account's balance at the start of the current phase and its peak so far (highest end-of-day balance for `end_of_day` trailing, highest intraday equity for `intraday`), both NinjaTrader balances; the form prefills the current balance but the user confirms them, and `peak < start` is refused. The confirmation shows the resulting liquidation level to check against Lucid's dashboard. The runner raises its peak to NinjaTrader's balance at each session close (and on each account message for intraday trailing) and journals each raise; on start it uses the highest of `armed.peak_balance` and the journalled peaks. If an `end_of_day` session close passed while the server was down, the account halts ("confirm balances") until the user confirms or re-enters the peak and the liquidation level. Dry runs start fresh.
- **Stricter of (DEC-4b):** drawdown remaining and today's P&L are computed from the app's own state and from NinjaTrader's balance and P&L against the same floor; the smaller distance wins. The Live page shows NinjaTrader's absolute balance and every distance in dollars.
- **Pinned inputs (DEC-4d):** arming stores the account's `dll_on` and the hashes of its rules file and `instruments.yaml`; if `accounts.yaml` disagrees on `dll_on`, or either file's hash changes, the runner halts the account until it is re-armed (like a changed strategy file).
- **Warm-up (DEC-4e):** on start or restart, `Ctx.bars` fills from the Parquet store plus today's live bars, and `init` + `step` replay today's session with actions suppressed; at each replayed bar `ctx.position` and `ctx.account` come from today's journalled fills. A replayed action that differs from the journalled one for that bar halts the account ("warm-up diverged"). Live decisions start at the next closed bar.
- **Reconciliation:** on start and every reconnect. Any mismatch halts the account until the user resolves it. NinjaTrader is the source of truth for positions.
- **Failure handling:**

| Situation | Behavior |
|---|---|
| App, container or link lost | No new entries; open positions keep broker-side stop and target. |
| Heartbeat lost with a position | Keep the bracket (default) or flatten: the add-on's `flatten_on_disconnect_seconds`, since Python cannot act once the link is down. (`FENRIR_HEARTBEAT_POLICY` is retired.) |
| No LSE bar for N seconds in session, or LSE drifts from fills | Pause new entries and alert. |
| 16:40 America/New_York | Add-on flattens and cancels its managed accounts on its own, Python connected or not. |
| Safety margin hit (DEC-4c; margin default 80%): today's loss reaches margin × the daily loss limit (when on), or the distance to the current drawdown floor falls to (1 − margin) × what it was at session start; recomputed each bar | Halt the account for the day. |
| Strategy, rules or `instruments.yaml` hash changed while armed | Halt the account. |
| Kill switch | Flatten all, cancel all, disarm all. |

- **Journal:** SQLite `<private_dir>/fenrir.db` records every bar decision, action, rejection, fill, alert and halt.

### NinjaTrader add-on

HTTP + WebSocket server inside NinjaTrader on a configurable port, shared token on every request. Lists accounts, positions and working orders; submits brackets (market entry + OCO stop/target); flatten, cancel, flatten-all; streams account, position, order, fill and a 2 s heartbeat. Duplicate client order IDs are ignored, and the seen IDs persist to a file on the NinjaTrader machine (last 7 days) so dedupe survives a restart. It acts only on the accounts listed in its config (`managed_accounts`); orders for any other account are refused (and not recorded as seen, so a retry is not a false duplicate), the 16:40 ET flatten and `/flatten-all` touch managed accounts only, and it logs loudly at start when the list is empty. Flattens and cancels at 16:40 ET by itself. With `flatten_on_disconnect_seconds` set (default off, matching "keep the bracket"), it flattens managed accounts holding a position after no `/stream` client for that long. On every entry fill it sets the OCO stop and target quantity to the total filled so far (placing them on the first fill, resizing on later ones); if the entry is not complete after 2 s it cancels the remainder, and fills arriving after the cancel are still protected. No market data, no decisions; forwards to NinjaTrader's API only. Shipped as an importable NinjaScript zip built from the repo.

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

Users: `git clone`, `docker compose up`, open the launch link it prints (`docker compose logs` shows it again), then enter the LSE key and NinjaTrader address and token on the Settings page. Multi-stage image (Node builds UI; Python runtime with `uv`, built UI, Claude Code, `git`), `restart: unless-stopped`. The whole repo is bind-mounted at `/app` (DEC-6), with `compose.yaml`, `Dockerfile`, `.dockerignore`, `.git/config` and `.git/hooks` mounted read-only over it; the server's code and built UI come from the image (`/opt/fenrir`), so edits under `/app/src` or `/app/web` take effect only when the user rebuilds (review them with `git log -p origin/main..` and `git diff` first, since Claude commits from the terminal). Secrets, `fenrir.db` and the session key live in a root-only named volume at `/var/lib/fenrir`, never in the repo; Claude Code's login lives in a named volume owned by `claude`. At start the server refuses to open a terminal unless it runs as root with `FENRIR_PTY_USER` naming a non-root user and the private dir is outside `/app`, root-owned, mode 0700; it logs why. The image ignores a repo `.env`; the Settings page warns when one holds secrets and offers to import them. NinjaTrader at `host.docker.internal:<port>` on the same Windows host, or the VM's IP from macOS. Live trading also needs NinjaTrader 8 with the add-on imported; the README covers import, VM IP and Windows Firewall.

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
- **Security:** server rejects requests without the session cookie, foreign Hosts, cross-origin non-GET requests and foreign WebSocket Origins; in the image, a test checks the terminal user cannot read the private volume.
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
- Add a CLI path, API path, file or environment leak that lets the Docker terminal arm, disarm, or reach the session key, secrets or `fenrir.db`.
- Import or execute a repo or `data/` file in a root process.
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
4. Arming is refused (409) without a backtest matching the strategy and rules file hashes, account, contract and `dll_on`; arming a live account needs confirmation.
5. In dry run, sim and live, every bracket sent has a stop; a bracket whose stop-out would breach a limit is rejected and journalled.
6. With Python disconnected, the add-on flattens and cancels all managed accounts at 16:40 ET on a Sim account (manual checklist).
7. The server returns 401 without the session cookie and 400 for a foreign Host, and refuses WebSocket handshakes without the cookie or from a foreign Origin with HTTP 403; in Docker, the terminal cannot read the session key, secrets or `fenrir.db`.
8. From a clean clone, `docker compose up` serves the app at `http://localhost:8000` through the printed launch link.

## Risks

| Risk | Mitigation |
|---|---|
| LSE has no real-time CME futures on the current tier | Gate item 3 before plan 04; fetch is the only LSE-aware code. |
| Lucid rules change or summaries are wrong | Rules from Lucid's site; `verified_on` staleness warning. |
| Overfitting from many strategies on the same data | Hold-out; ranking never uses it; `holdout_gap` flag. |
| Backtest fills better than live | Conservative fill model; dry run and sim first; journal for live vs backtest. |
| Python fold too slow | Estimated from a sample (Open Question 5): about 3 h for one ORB backtest. 10.4 loads bars once and caps workers by memory; the attempt length is Open Question 6. |
| Worker memory | About 1.5 GB per spawn worker (24 GB on 16 cores) would exceed a typical Docker Desktop memory limit; 10.4 caps the worker count. |
| Browser terminal as attack surface | Loopback binding, Host check, one-time launch link → HttpOnly cookie, Origin checks; in Docker the terminal runs as `claude` and nothing it writes runs as root. |
| Host goes down with a position open | Broker-side brackets; add-on flattens at 16:40 on its own. |

## Open Questions

1. **Live futures on LSE (gate item 3).** On 2026-10-10 the socket reported tier `registered` with no `ES.F`/`NQ.F` among its streamable symbols. If futures ticks don't arrive in market hours, what's the live source: an LSE tier upgrade, Databento, or something else? This blocks plans 04, 21 and 22.
2. **Gate items 4–5** (live vs historical bars; LSE vs NinjaTrader fills) have not been run.
3. **ES 2023-03 roll** is ambiguous in the data; the fixed roll window covers it, but it isn't confirmed.
4. **Pro, Direct and Daily rules** are not entered yet, and `automation_allowed` has not been checked for them on Lucid's site.
5. **Fold performance** (09.2, sampled 2026-10-11 on a 16-core, 64 GB Mac; the full 3 h run was skipped). `fenrir backtest strategies/orb.py --account lucid-flex-50k --contract MNQ`: 3,386,345 NQ bars load in 12.2 s; tuning 2016-06-01 → 2025-10-09 is 2,265 attempts, hold-out 2025-10-10 → 2026-10-09 is 243. ORB risks about $20 to make $40 against a $3,000 target and $2,000 drawdown, so most attempts run to the end of the data (`no_result`); two sampled attempts took 160–170 s each, about 52 µs per bar, so about 50 CPU-hours, about 3 h wall on 16 workers. 16 spawn workers held about 24 GB RSS (largest 1.65 GB). Parent RSS was not sampled; it holds one full series while planning (about 2.2 GB, see Market data), so the total is about 26 GB. **No-go** on both thresholds: 60 s wall time and 6 GB total memory (chosen to fit a modest Docker Desktop limit). Follow-up is folded into 10.4 (#27): load once per process and cap workers to fit memory. Load is not the bottleneck, though: the fold over years of bars per attempt is.
6. **Attempt length.** An attempt runs until pass, payout or breach, or the end of the data, so a slow strategy costs O(trading days × bars). Capping each attempt at a horizon (say 120 trading days, then `no_result`) would cut a full run from hours to minutes and match how long a trader keeps paying for one evaluation, but it changes the report's meaning and the cache key (ask first).
