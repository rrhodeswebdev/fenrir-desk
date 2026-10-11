# Implementation Plan: Fenrir Desk v1, remaining work (plans 09–24)

**Updated:** 2026-10-11 · Spec: root `SPEC.md` · Contracts: `docs/contracts.md`

## Overview

Plans 01–03 and 05–08 are merged. Plan 09 is built on `feat/09-attempts-report` but not merged. This plan re-cuts plans 09–24 into issue-sized tasks (S or M) after checking every plan against `SPEC.md`, the cross-plan contracts (now `docs/contracts.md`) and the code on `main`.

**Tasks are tracked in GitHub Issues** (repo `rrhodeswebdev/fenrir-desk`). Each issue body holds the acceptance criteria, verification and dependencies. The source plan files were deleted on 2026-10-11, so each issue is the whole task; "Plan NN Task N" and "plan NN:line" in issues are historical references. "README" contract references in issues mean `docs/contracts.md`.

## How the plans were re-cut

- The plan's own task boundaries are kept unless a task was L/XL (plan 14 T1–8, plan 22 T4, plan 24 T4 were split; plan 11 T2+T3 merged).
- `compare.py` and CLI ranking move from plan 14 into plan 10 (SC3 needs a CLI ranking, which no plan had).
- New tasks fill spec gaps: roll-window block live, warm-up of `Ctx.bars`, pin `dll_on` at arming, grid series memoization, real-socket security test, terminal-proof token delivery, add-on zip build, rules for Pro/Direct/Daily, data-gate items 3–4.
- Every plan's `Spec:` line still cites the superseded v1 design doc; read `SPEC.md` instead.

## Corrections carried into issues (drift found in review)

| Plan | Correction |
|---|---|
| 09 | Task 5 profile script calls `_load` with 4 args (needs 5); measure worker memory, not just the parent's. `failure_reasons` must always carry all three keys. |
| 10 | Keep `except (ValueError, OSError)` in the CLI and the missing-file test. Drop `find_report` (unused; ignores contract and `dll_on`). Each grid item reloads 2.2 GB of bars and starts a new pool, which breaks the "load once per process" contract. |
| 11 | WS `close(4401)` before `accept()` is an HTTP 403 in a real browser (SC7 only holds in TestClient). Token in `index.html` is readable by `curl` from the terminal. |
| 12 | `errorText.ts` belongs here, not in plans 14–16. Selected table rows use Night Deep, not idle. SPEC E2E command is wrong; use `npm --prefix web run e2e`. |
| 13 | `.terminal` locator matches two elements (xterm's own root); use `data-testid`. Clear `ws.onclose` before close. |
| 14 | Show item errors in the row, not a tooltip. Report page adds gross per attempt, `dll_on`, params. `jobs()` reads under the lock. Pin `FENRIR_HOLDOUT_MONTHS=12` in Playwright. |
| 15 | Saving drops YAML comments and reformats values. Form misses `min_day_profit`, `max_amount`, `max_payouts`, `funded.scaling`. |
| 16 | "Outside calendar" notice is Ember, not Red. |
| 17 | Use `uv run fenrir`; test `--flags` against `--help`; add roll window, bracket-while-open, DLL halt, `holdout_gap`. |
| 18 | Whole-repo mount vs SPEC's five mounts. `FENRIR_BIND_ALL` belongs to 18, not 11. Run a backtest inside the container (memory). Link `addon/INSTALL.md`, not `addon/README.md`. |
| 04 | `stream_ticks` uses a guessed API. Real: `from lse import LSE`, `stream_async`, skip `replay` and no-volume ticks, round to 0.25, catch `LSEError`. Needs a monkeypatched-client test. |
| 19 | 16:40 flatten timer only starts if the token is valid and the server binds: start it first. Parse `flatten_at` once. Release a claimed client id when submit throws. Token via `secrets`/`openssl`, not `Get-Random` base64. Build the zip from the repo. |
| 20 | Fake add-on timestamps must be `…sss Z`. `reconcile` must flag stray working orders while a position is held. Test files `test_names.py`, `test_client.py`. |
| 21 | Test helpers fail under `--import-mode=importlib`: use `tests/live/live_helpers.py` + `pythonpath`. No roll-window check. Status `max_minis` ignores funded scaling. `lse_connected` set before any bar. `LiveHub` opens `data/fenrir.db` at app build. Needs `pytest-asyncio` (plan 20). |
| 22 | Gate item 5 script never matches a fill to a bar (timestamp formats) and passes with zero compared. Kill switch reports ok with no NT client. Drift pause cannot be cleared from the UI. T4 too big. |
| 23 | Disarm is not journalled. `dll_on` changes after arming silently apply on restart. |
| 24 | Safety mark uses a shadow (DESIGN.md: inset only). Dial ignores reduced motion. MISMATCH rows in Red. "Full journal" link. T4 too big. |

## Decisions needed (each is a `decision` issue; blocking tasks wait on it)

1. **Terminal isolation.** From the Workbench terminal Claude can read the session token (`curl 127.0.0.1:8000/`), the NinjaTrader token in `.env`, and write `data/fenrir.db` (`armed` table). SPEC "Never" forbids the first. Options: one-time launch code → HttpOnly cookie; secrets outside the repo; terminal as another user.
2. **Rejected WebSocket behaviour.** 4401 after `accept()` vs amend SC7 to "handshake rejected (403)".
3. **Rules YAML on save.** `ruamel.yaml` round-trip (new dep), a `notes:` field, or accept losing comments.
4. **Live limits.** Account starting state at arming; whether NinjaTrader balance/drawdown feeds "stricter of"; safety margin when DLL is off (no-op on every Flex account today); pin `dll_on` in `armed`; warm-up source for `Ctx.bars` and strategy state.
5. **Add-on behaviour.** 16:40 flatten and flatten-all hit every NinjaTrader account; add-on-side flatten on lost link; partial entry fills; persist seen client ids across restarts.
6. **Docker mounts.** Whole repo at `/app` vs the five bind mounts in SPEC.
7. **Live data source** (only if data-gate items 3–4 fail).

## Task list (GitHub issue index)

Issues #10–#99, grouped into milestones v1.1–v1.6, with "blocked by" links between them. Size S = 1–2 files, M = 3–5.

### Phase 0: Decisions, data gate, rules
- [ ] #10 DEC-1 Terminal isolation for tokens and `armed` · ask-first
- [ ] #11 DEC-2 WebSocket rejection code (SC7) · ask-first
- [ ] #12 DEC-3 Rules YAML comment preservation · ask-first
- [ ] #15 DEC-4 Live limit open points · ask-first
- [ ] #14 DEC-5 Add-on behaviour questions · ask-first
- [ ] #13 DEC-6 Docker mounts · ask-first
- [ ] #16 GATE-3 Run data-gate item 3 in market hours · S
- [ ] #17 GATE-4 Run data-gate item 4 over a full session · S · after GATE-3
- [ ] #18 DEC-7 Live data source (if GATE-3/4 fail) · ask-first
- [ ] #19 RULES-1 Enter Lucid Pro rules · S · ask-first
- [ ] #20 RULES-2 Enter Lucid Direct rules · S · ask-first
- [ ] #21 RULES-3 Enter Lucid Daily rules · S · ask-first

### Phase 1: Backtester complete (09, 10, 17)
- [ ] #22 09.1 Land attempts-report: rebase, all failure keys, SPEC commands · S
- [ ] #23 09.2 Measure a full evaluate (time + worker memory) · S
- [ ] #24 10.1 Globbed grids and `expand_grid` · S
- [ ] #25 10.2 Result cache by report key (+ SC2 byte-identical test) · S
- [ ] #26 10.3 CLI backtest/evaluate over grids with the cache · S
- [ ] #27 10.4 Load each series once per process across a grid · M
- [ ] #28 10.5 Compare ranking (tuning only) + `fenrir compare` CLI · M
- [ ] #29 17.1 fenrir-strategy project skill and drift tests · S

**Checkpoint A:** SC1, SC2 and the CLI half of SC3 hold from the terminal; fold speed and memory recorded in SPEC.

### Phase 2: Server, web shell, terminal (11, 12, 13)
- [ ] #30 11.1 Pure auth checks and token injection · M · ask-first (deps)
- [ ] #31 11.2 `create_app`: Host check, health, dev token, SPA fallback · M
- [ ] #32 11.3 `fenrir serve` bound to 127.0.0.1 · S
- [ ] #33 11.4 Real-socket security test (SC7) · S · after DEC-2
- [ ] #34 11.5 Token delivery the terminal cannot read · M · after DEC-1
- [ ] #35 12.1 Vite app scaffold and token-aware API client · M · ask-first (deps)
- [ ] #36 12.2 DESIGN.md theme, shadcn init, Button control · M · ask-first (deps)
- [ ] #37 12.3 Index-rail shell, Page, StateText, page stubs · M
- [ ] #38 12.4 Playwright smoke against `fenrir serve` · S
- [ ] #39 13.1 Pure env scrub, shell choice, message parsing · S
- [ ] #40 13.2 PTY over `/ws/terminal` · M · ask-first (pywinpty)
- [ ] #41 13.3 xterm.js on the Workbench · M · ask-first (deps)

**Checkpoint B:** SC7 against a real server; terminal runs `claude`; no token reachable from it.

### Phase 3: Backtests, Rules, Data pages (14, 15, 16)
- [ ] #42 14.1 Pure job state · S
- [ ] #43 14.2 JobRunner: persistence, resume, listeners · M
- [ ] #44 14.3 Backtests API and `/ws/jobs` · M
- [ ] #45 14.4 Backtest run form and page wiring · M · ask-first (shadcn)
- [ ] #46 14.5 Live job list over `/ws/jobs` · M
- [ ] #47 14.6 Comparison table by account and by strategy · S · ask-first (shadcn)
- [ ] #48 14.7 Report page with histogram and equity curves · M · ask-first (lightweight-charts)
- [ ] #49 14.8 Workbench latest-report pane · S
- [ ] #50 14.9 Playwright backtest flow on fixture bars · M
- [ ] #51 15.1 Rules API · M · after DEC-3
- [ ] #52 15.2 Rules list with staleness warning · S
- [ ] #53 15.3 Field-by-field rules editor · M · ask-first (shadcn)
- [ ] #54 15.4 Payout extras and scaling tiers in the form · S
- [ ] #55 15.5 Playwright rules flow on a disposable copy · S
- [ ] #56 16.1 Pull runner · S
- [ ] #57 16.2 Data API · S
- [ ] #58 16.3 Data page · M

**Checkpoint C:** SC3 from the UI; a backtest runs end-to-end in Playwright.

### Phase 4: Docker (18)
- [ ] #59 18.1 Bind all interfaces only inside the image · S
- [ ] #60 18.2 Multi-stage image, loopback compose, run guide · M · after DEC-1, DEC-6

**Checkpoint D:** SC8 from a clean clone; a backtest runs inside the container.

### Phase 5: NinjaTrader add-on and bridge (19, 20) — can run in parallel with Phases 1–4
- [ ] #61 19.1 Add-on config, HTTP server, token auth, `/accounts` · M · Windows
- [ ] #62 19.2 Positions and working orders · S · Windows
- [ ] #63 19.3 OCO brackets with client-id dedupe · M · Windows · ask-first · after DEC-5
- [ ] #64 19.4 Flatten, cancel, flatten-all · S · Windows · ask-first
- [ ] #65 19.5 Event stream and 2 s heartbeat · M · Windows
- [ ] #66 19.6 Independent 16:40 ET flatten · S · Windows · ask-first
- [ ] #67 19.7 Build the importable zip from the repo · S
- [ ] #68 19.8 Sim checklist and README add-on section · S · Windows
- [ ] #69 20.1 NinjaTrader instrument names · S
- [ ] #70 20.2 Pure reconciliation · S · ask-first
- [ ] #71 20.3 Fake add-on and async client · M · ask-first (deps)

**Checkpoint E:** SC6 on a Sim account with Python stopped; bridge tests green against the fake.

### Phase 6: Live trading (04, 21–24)
- [ ] #72 04.1 Pure tick → 1m bar builder · S
- [ ] #73 04.2 LSE live tick stream and `stream_bars` · M · blocked on GATE-3 / DEC-7
- [ ] #74 04.3 Real stream check · S · manual · blocked
- [ ] #75 21.1 Journal and armed tables in SQLite · S
- [ ] #76 21.2 Pure runner core per bar · M · ask-first
- [ ] #77 21.3 No entries in the roll window · S · ask-first
- [ ] #78 21.4 Day roll, halts and stale-data pause · S · ask-first
- [ ] #79 21.5 Warm-up of `Ctx.bars` and strategy state · M · after DEC-4, DEC-7
- [ ] #80 21.6 LiveHub shell and server lifespan · M
- [ ] #81 21.7 Live status and journal API · S
- [ ] #82 21.8 Dry run on live data · S · manual · blocked
- [ ] #83 22.1 Send accepted actions and apply fills · M · ask-first
- [ ] #84 22.2 NinjaTrader account figures and mode check · M · ask-first · after DEC-4
- [ ] #85 22.3 Link loss, reconcile, resolve, replay · M · ask-first
- [ ] #86 22.4 Hub sends orders and replays fills · M · ask-first
- [ ] #87 22.5 Hub follows the add-on stream; kill switch · M · ask-first
- [ ] #88 22.6 Kill switch and resolve endpoints · S · ask-first
- [ ] #99 22.7 Gate item 5 and the sim checklist · M · manual · blocked
- [ ] #89 23.1 Pure arm decision · S
- [ ] #90 23.2 Arming API and no-CLI guard · M
- [ ] #91 23.3 Pin `dll_on` at arming · M · ask-first · after DEC-4
- [ ] #92 23.4 Strategies API · S
- [ ] #93 23.5 Strategies page with arm, disarm, live confirmation · M
- [ ] #94 24.1 `.env` update and secret masking · S
- [ ] #95 24.2 Settings API · M
- [ ] #96 24.3 Settings page · M
- [ ] #97 24.4 Live page strip and kill switch · M
- [ ] #98 24.5 Live focus, dial, meters, sleeves, journal tape · M

**Checkpoint F (v1 complete):** SC4 and SC5 in dry run, sim and live; all eight success criteria checked.

## Parallel lanes

- **Now:** DEC-1…6, GATE-3, RULES-1…3, 09.1, 19.1, 20.1, 04.1.
- **Backtest lane:** 09 → 10 → 17; 14 needs 10 and 12.
- **UI lane:** 11 → 12 → 13/15/16 (15 and 16 don't need 10).
- **Broker lane:** 19 → 20 (Windows box needed for 19).
- **Live lane:** needs the backtest, UI and broker lanes plus GATE-3 (or DEC-7).

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LSE has no live futures on the current tier | High: blocks 04.2, 21.5, 21.8, 22.7 | GATE-3 first; DEC-7; `stream_bars` stays the only seam |
| Claude in the terminal reaches the token, `.env` or `armed` | High: breaks the arming boundary | DEC-1 before 13.2 ships and before 23.2 |
| Fold memory: about 2.2 GB per worker × CPUs | High: OOM in Docker, UI grids unusable | 09.2 measures; 10.4 memoizes and reuses the pool |
| Add-on 16:40 flatten silently never runs | High: positions held past flat-by | 19.6 starts the timer first and independently |
| Gate item 5 passes with nothing compared | Med | 22.7 requires ≥10 fills compared |
| Pro/Direct/Daily rules not entered | Med: SC1 says "every Lucid account file" | RULES-1…3 |

## Open questions

See "Decisions needed". Also: ES 2023-03 roll is unconfirmed (the roll window covers it; no task).
