# Fenrir Desk

A local-only, browser-based workbench for prop traders. Fenrir Desk helps you design, backtest, and run trading strategies built around the rules of specific prop firm challenges, with the goal of passing evaluations and reaching payouts as quickly and safely as possible.

> **Status:** early development. The core, rules, strategy contract, history pull and single-attempt backtest are built; the UI and live trading are not. Nothing here is usable yet.

The full v1 spec is [SPEC.md](SPEC.md).

## Goals

- **Prop firm rules library.** A structured, versioned library of challenge and funded-account rules (profit targets, daily loss limits, trailing drawdown, consistency rules, minimum trading days, contract limits, payout requirements). v1 covers Lucid Trading only.
- **Rule-aware strategy generation.** Claude Code writes and tunes deterministic strategies that treat a firm's rules as hard constraints, optimizing for time-to-pass and time-to-payout instead of raw profit.
- **Backtesting on deep history.** Pull historical market data from an external provider so strategies can be tested across many years and market regimes, and simulate the firm's rules bar by bar.
- **NinjaTrader execution.** Connect to NinjaTrader to place and manage trades for live or sim accounts.
- **Local only.** Runs on your machine and is served to your browser from localhost. No hosted backend, no accounts, and your credentials and data never leave your computer.

## Stack

- **Backend and CLI:** Python 3.12, `uv`, FastAPI, Typer, Polars + Parquet.
- **UI:** Vite, React, TypeScript, TanStack Router + Query, Tailwind, shadcn/ui.
- **Market data:** London Strategic Edge (LSE), history and live.
- **Execution:** a C# NinjaScript add-on in NinjaTrader 8 (Windows), over local HTTP + WebSocket.

## Development

```
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run fenrir data pull        # needs FENRIR_LSE_API_KEY in .env
uv run fenrir data summary
```

`docker compose up` is planned for running the whole app.

## Disclaimer

Fenrir Desk is a personal software project, not financial advice. Trading futures and other leveraged products carries substantial risk of loss. Prop firm rules change often; always verify the current rules with the firm before relying on anything in this library. Use at your own risk.

## License

[MIT](LICENSE)
