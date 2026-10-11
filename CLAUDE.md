# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Commits

**Use [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/#specification).**

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

- Types: `feat` (new feature → MINOR), `fix` (bug fix → PATCH); also `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `perf`, `style`.
- Scope is a noun for the area changed, e.g. `feat(backtester): ...`, `fix(addon): ...`.
- Description: imperative, lowercase, no trailing period.
- Breaking changes: `!` before the colon and/or a `BREAKING CHANGE: <description>` footer (→ MAJOR).
- Footers use `Token: value` form (e.g. `Refs: #12`).

## 6. Directory Map

Every directory with its own responsibility has a short `CLAUDE.md` saying what it handles, what it does not, and its entry points. Read it before working in that directory. When you create a directory, add its `CLAUDE.md` and a row here in the same commit; when a directory's job changes, update both.

| Directory | Handles |
|---|---|
| `assets/brand/` | Logo, app icon and brand guide (palette, type). Source of truth for visual identity. |
| `docs/` | Data-gate findings, cross-plan contracts (`contracts.md`), Lucid rule mechanics. Not runtime code. |
| `tasks/` | `plan.md`: ordered index of the remaining v1 work; each task is a GitHub issue. |
| `design/` | Static design prototypes (synthetic data) that set the visual system; `DESIGN.md` at the root records it. |
| `src/fenrir/` | The `fenrir` Python package; `cli/main.py` is the CLI entry, `settings.py` loads `.env`. |
| `src/fenrir/cli/` | Typer CLI, one module per command group. Thin shell over the core. |
| `src/fenrir/core/` | Pure shared domain types: `Bar`, actions, contract specs, rollover, trading days. |
| `tests/` | pytest suite mirroring `src/fenrir/`; `fixtures/` holds shared test data. |
| `scripts/` | One-off scripts run by hand (LSE data gate). Never imported by the app. |
| `src/fenrir/rules/` | Rules schema, loader, account state and pure rule functions. |
| `rules/` | Prop-firm rule YAML files, one per account type and size, entered from the firm's own site. |
| `src/fenrir/strategy/` | Strategy contract (`Ctx`, `Position`, `Strategy`) and the loader that imports and validates strategy files. |
| `strategies/` | Strategy files written by Claude; each exports `MARKET`, `PARAMS`, `init`, `step`. Contract in `strategies/CLAUDE.md`. |
| `src/fenrir/data/` | LSE history pull, Parquet store, front-month bar loading, live bar building. Only `lse.py` knows LSE. |
| `src/fenrir/backtest/` | Fill model, single-attempt fold, attempts across days, report JSON, grid expansion and result cache. |
| `src/fenrir/bridge/` | Python side of the NinjaTrader add-on: instrument names, add-on client, reconciliation. |
| `src/fenrir/live/` | Live runner: SQLite trade journal and armed table (`<private_dir>/fenrir.db`); later per-bar decisions, limits and halts. |
| `src/fenrir/server/` | FastAPI shell and its state: pure backtest-job state and session-cookie/Origin/CSRF auth now; app, PTY and routes arrive in plans 11–16. |
| `web/` | React UI (Vite, React 19, TypeScript strict). `src/api.ts` is the only server client (session cookie, no token); `src/errorText.ts` turns errors into text. |
| `data/` | Gitignored runtime data: Parquet bars, manifest, backtest results, jobs, SQLite. Only `CLAUDE.md` is committed. |
| `.claude/skills/fenrir-strategy/` | Project skill teaching Claude the strategy loop (rules → data → strategy → backtest → report), tuning period only. |

All UI work follows root `PRODUCT.md` (who it is for, voice, principles) and `DESIGN.md` (tokens, components, colour and motion rules).

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
