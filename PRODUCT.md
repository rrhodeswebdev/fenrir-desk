# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One futures prop trader per install. Built first for the author's own trading; anyone can clone it, but the app does not design for beginners. The user knows prop firm rules, futures contracts and NinjaTrader.

Two usage scenes:

- **Desk, market hours.** Large monitor(s). The Live page stays open on a second screen and is glanced at, not stared at. The job: know at a glance whether every armed account is safe, and act instantly (disarm, kill switch) when it is not.
- **Evenings, research.** Laptop or desktop outside market hours. Workbench with Claude Code in the terminal, backtests, comparing strategies across Lucid account types.

## Product Purpose

Fenrir Desk is a local-only web app where the trader uses Claude Code to design, backtest and run futures strategies (ES, MES, NQ, MNQ) whose only goal is passing Lucid Trading evaluations and reaching payouts on funded accounts. Claude writes deterministic strategy code; the app backtests it against the firm's rules and, once the user arms it, runs that same code live through NinjaTrader.

Success: a strategy's probability of passing and reaching a first payout, with expected net $ per attempt after fees, is known before it is armed; armed strategies trade with every position protected by a broker-side stop.

## Positioning

Optimizes for time-to-pass and time-to-payout under a specific firm's rules, not raw profit. Rules are hard constraints simulated bar by bar; results are probabilities across every start day, with a held-out period that ranking never uses. The same pure strategy code runs in backtest and live.

## Operating Context

- Runs locally via `docker compose up`, served at `http://localhost:8000`; NinjaTrader 8 runs on Windows (host or VM).
- Pages: Workbench (terminal + latest report), Rules, Data, Backtests, Strategies, Live, Settings (spec §10).
- Claude Code works in the built-in terminal; it can backtest but can never arm. Arming is a deliberate user action; arming a live account requires a confirmation showing the backtest summary.
- Live modes: dry run, sim, live. Failures halt accounts and raise alerts; the kill switch flattens, cancels and disarms everything.

## Capabilities and Constraints

- Lucid Trading only (Pro, Flex, Direct, Daily); instruments ES, MES, NQ, MNQ.
- One armed strategy per account; single user; no login beyond a local session token.
- Market data from London Strategic Edge; execution through a NinjaTrader add-on.
- Stack: Vite, React, TypeScript, TanStack Router and Query, shadcn/ui, Tailwind CSS v4; charts with TradingView Lightweight Charts.
- Terminology: account type (e.g. `lucid-flex-50k`), attempt, evaluation, funded, breach, payout, hold-out, tuning period, arm/disarm, halt, kill switch, dry run / sim / live.

## Brand Commitments

- Name: Fenrir Desk. Logo, mark, wordmark, app icon and palette in `assets/brand/` (guide: `assets/brand/README.md`).
- Brand type: Chakra Petch (wordmark), IBM Plex Mono (tagline).
- Theme: dark everywhere. There is no light theme.
- Voice: terse operator. Short, factual, numbers first, like a trading terminal ("Halted: 81% of daily loss used.").

## Evidence on Hand

No real backtest results, trade history or verified Lucid rule values exist yet. Lucid values must be entered from Lucid's own site; never fabricate rule numbers, results or performance claims.

## Product Principles

1. **Risk is never ambiguous.** Distance to every limit, halts and armed state are visible at a glance from across the desk.
2. **Probabilities, not stories.** Show pass/payout rates and distributions with their caveats; never a single lucky equity curve as proof.
3. **Deliberate power.** Dangerous actions (arm live, kill switch) are one clear step with an honest confirmation, never hidden and never casual.
4. **Same truth everywhere.** Backtest, journal and live views use the same terms and numbers so they can be compared directly.
5. **Terminal-grade density.** Built for an expert; dense and terse beats explanatory.
