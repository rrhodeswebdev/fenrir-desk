# src/fenrir/backtest/

Backtesting: the fill model, one attempt as a fold over bars, attempts across every trading day, the report JSON, grids and the result cache.

- Entry points: `fills.py` (fill prices), `attempt.py` (`run_attempt` → `AttemptResult`); later `attempts.py`, `report.py`, `run.py` (`run_backtest`), `grid.py`, `cache.py` (plans 09–10).
- Pure vs shell: everything is pure except `run.py` (loads files, process pool) and `cache.py` (reads/writes `data/results/`).
- Fill model is conservative by design (next-bar-open entry +1 tick, stop before target, 1 tick on stops/market exits). Do not make it more optimistic.
- Not here: rule logic (`src/fenrir/rules/`), strategy loading (`src/fenrir/strategy/`), bar storage (`src/fenrir/data/`).
- Tests: `tests/backtest/`.
