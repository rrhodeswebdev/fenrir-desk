# src/fenrir/backtest/

Backtesting: the fill model, one attempt as a fold over bars, attempts across every trading day, the report JSON, grids and the result cache.

- Entry points: `fills.py` (fill prices), `attempt.py` (`run_attempt` → `AttemptResult`), `attempts.py` (`periods`: tuning/hold-out split), `report.py` (`period_result`, `build_report`, `report_key`), `run.py` (`run_backtest`, `load_account_choices`, `net_per_attempt`; reads `<data_dir>/accounts.yaml`), `grid.py` (`expand_grid` → `GridItem`s: strategy globs × account globs × contracts); later `cache.py` (plan 10).
- Pure vs shell: everything is pure except `run.py` (loads strategy, rules, bars and `accounts.yaml`; runs the process pool), `grid.py` (globs files, loads each strategy for its MARKET) and `cache.py` (reads/writes `data/results/`).
- Fill model is conservative by design (next-bar-open entry +1 tick, stop before target, 1 tick on stops/market exits). Do not make it more optimistic.
- Not here: rule logic (`src/fenrir/rules/`), strategy loading (`src/fenrir/strategy/`), bar storage (`src/fenrir/data/`).
- Tests: `tests/backtest/`.
