# Fenrir Desk

A local-only, browser-based workbench for prop traders. Fenrir Desk helps you design, backtest, and run trading strategies built around the rules of specific prop firm challenges, with the goal of passing evaluations and reaching payouts as quickly and safely as possible.

> **Status:** early development. Nothing here is usable yet.

## Goals

- **Prop firm rules library.** A structured, versioned library of challenge and funded-account rules (profit targets, daily loss limits, trailing drawdown, consistency rules, minimum trading days, contract limits, news restrictions, payout requirements) for a variety of prop firms.
- **Rule-aware strategy generation.** Generate and tune strategies that treat a firm's rules as hard constraints, optimizing for time-to-pass and time-to-payout instead of raw profit.
- **Backtesting on deep history.** Pull historical market data from an external provider so strategies can be tested across many years and market regimes, and simulate the firm's rules bar by bar.
- **NinjaTrader execution.** Connect to NinjaTrader to place and manage trades for live or sim accounts.
- **Local only.** Runs on your machine and is served to your browser from localhost. No hosted backend, no accounts, and your credentials and data never leave your computer.

## Open questions

- **Market data source.** An external provider is preferred for deep historical data. Pulling candles from NinjaTrader is a fallback option.
- **NinjaTrader bridge.** How the app talks to NinjaTrader (for example, a NinjaScript add-on exposing a local socket or HTTP API).
- **Tech stack.** Not chosen yet.

## Disclaimer

Fenrir Desk is a personal software project, not financial advice. Trading futures and other leveraged products carries substantial risk of loss. Prop firm rules change often; always verify the current rules with the firm before relying on anything in this library. Use at your own risk.

## License

[MIT](LICENSE)
