# src/fenrir/server/

The FastAPI shell and the state behind its APIs. API shapes in `docs/contracts.md` (Plans 11–16).

- Entry points: `jobs.py` (pure backtest-job state: `Job`, `JobItem`, `new_job`, `update_item`, `unfinished`, `restart`, `progress_step`, `to_json`/`from_json`); `auth.py` (pure `session_ok`, `allowed_origins`, `origin_ok`, `csrf_ok`; FastAPI glue `require_session` for every `/api` router, `check_ws` for every `/ws/*` handler before `accept()`, else `await ws.close()` and return).
- Auth state: `auth.py` reads `app.state.session_key: str` and `app.state.allowed_origins: frozenset[str]` (from `allowed_origins(port, dev)`); `create_app` must set both. Cookie name: `fenrir_session` (`SESSION_COOKIE`).
- Pure vs shell: `jobs.py` and the `auth.py` predicates are pure. Later: `jobs_runner.py` (`JobRunner`: `data/jobs/<id>.json`, worker thread, WebSocket listeners; plan 14), and the app, launch link, PTY and routes (plans 11–16).
- Not here: running a backtest or the grid itself (`src/fenrir/backtest/`), rule logic (`src/fenrir/rules/`), the web UI (`web/`).
- Tests: `tests/server/`.
