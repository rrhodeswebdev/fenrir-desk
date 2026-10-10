# src/fenrir/core/

Pure shared domain types and calendar helpers used by data, rules, backtest and live code.

- Entry points: `bars.py` (`Bar`), `actions.py` (`Bracket`, `Flatten`, `Cancel`, `Action`), `instruments.py` (`ContractSpec`, `Roll`, `Instruments`, `spec`, `front_month`, `load_instruments`), `sessions.py` (`trading_day`, `at_or_after`).
- Pure vs shell: all pure except `load_instruments` (reads `instruments.yaml`).
- Prices are unadjusted front-month. Contract codes use a two-digit year (`NQZ26`).
- Not here: rule logic (`rules/`), strategy context types (`strategy/`), data loading (`data/`).
- Tests: `tests/core/`.
