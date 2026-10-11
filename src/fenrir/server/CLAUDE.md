# src/fenrir/server/

The FastAPI shell and the state behind its APIs. API shapes in `docs/contracts.md` (Plans 11–16).

- Entry points: `jobs.py` (pure backtest-job state: `Job`, `JobItem`, `new_job`, `update_item`, `unfinished`, `restart`, `progress_step`, `to_json`/`from_json`).
- Pure vs shell: `jobs.py` is pure. Later: `jobs_runner.py` (`JobRunner`: `data/jobs/<id>.json`, worker thread, WebSocket listeners; plan 14), and the app, auth, PTY and routes (plans 11–16).
- Not here: running a backtest or the grid itself (`src/fenrir/backtest/`), rule logic (`src/fenrir/rules/`), the web UI (`web/`).
- Tests: `tests/server/`.
