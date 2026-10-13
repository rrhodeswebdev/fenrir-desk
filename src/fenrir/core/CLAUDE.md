# src/fenrir/core/

Pure shared domain types and calendar helpers used by data, rules, backtest and live code.

- Entry points: `bars.py` (`Bar`), `actions.py` (`Bracket`, `Flatten`, `Cancel`, `Action`), `instruments.py` (`ContractSpec`, `Roll`, `Instruments`, `spec`, `front_month`, `load_instruments`, `read_instruments` (also the sha256 of the bytes read, pinned at arming)), `sessions.py` (`trading_day`, `at_or_after`).
- Pure vs shell: all pure except `load_instruments`/`read_instruments` (read `instruments.yaml` through `fenrir.files.read_regular`, at most 1 MiB, never a symlink or FIFO: the root server and pull read it, DEC-1).
- Prices are unadjusted front-month. Contract codes use a two-digit year (`NQZ26`).
- Not here: rule logic (`rules/`), strategy context types (`strategy/`), data loading (`data/`).
- Tests: `tests/core/`.
