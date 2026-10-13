# src/fenrir/backtest/

Backtesting: the fill model, one attempt as a fold over bars, attempts across every trading day, the report JSON, grids and the result cache.

- Entry points: `fills.py` (fill prices), `attempt.py` (`run_attempt` → `AttemptResult`), `attempts.py` (`periods`: tuning/hold-out split), `report.py` (`period_result`, `build_report`, `report_key`), `run.py` (`run_backtest`, `load_account_choices`, `net_per_attempt`; reads `<data_dir>/accounts.yaml`), `grid.py` (`expand_grid` → `GridItem`s: strategy globs × account globs × contracts), `cache.py` (`cache_key`, `cached_run`, `read_report`, `list_reports(settings, limit=None)`: `limit` reads only that many most recently written files (mtime, then name); `<data_dir>/results/<key>.json`, read through `fenrir.files.read_regular`, at most 16 MiB, never a symlink or FIFO), `compare.py` (`compare`, `latest_only`, `summarize`, `with_net`: rank by tuning results only).
- Pure vs shell: everything is pure except `run.py` (loads strategy, rules, bars and `accounts.yaml`; runs the process pool), `grid.py` (globs files, loads each strategy for its MARKET) and `cache.py` (reads/writes `data/results/`).
- Process state in `run.py`: the parent memoizes the period split and bar count (keyed by data dir, series, `data_version` and instruments) and frees the bars; each worker memoizes its bars and inputs; one spawn pool is reused across grid items. So a grid loads each series once in the parent and once per worker. `worker_cap` sizes the pool to 70% of the cgroup v2 memory limit, else physical memory. Runs are serialized by a lock; `release()` shuts the pool down and clears the memo.
- Fill model is conservative by design (next-bar-open entry +1 tick, stop before target, 1 tick on stops/market exits). Do not make it more optimistic.
- Not here: rule logic (`src/fenrir/rules/`), strategy loading (`src/fenrir/strategy/`), bar storage (`src/fenrir/data/`).
- Tests: `tests/backtest/`.
