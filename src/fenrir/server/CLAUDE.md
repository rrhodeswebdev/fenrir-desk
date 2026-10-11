# src/fenrir/server/

The FastAPI shell and the state behind its APIs. API shapes in `docs/contracts.md` (Plans 11–16).

- Entry points: `app.py` (`create_app(settings, session_key, launch_code, static_dir, *, port, dev)`: `HostGuard` middleware runs before routing on every HTTP and WebSocket request, 400 for a non-loopback Host, 307 from `127.0.0.1` to `localhost`; single-use `GET /launch?code=` sets the cookie and 303s to `/`; `GET /api/health`; SPA catch-all registered last, never for `api/` or `ws/`; pure `host_action`, `localhost_url`, `static_file`); `jobs.py` (pure backtest-job state: `Job`, `JobItem`, `new_job`, `update_item`, `unfinished`, `restart`, `progress_step`, `to_json`/`from_json`); `auth.py` (pure `session_ok`, `allowed_origins`, `origin_ok`, `csrf_ok`; FastAPI glue `require_session` for every `/api` router, `check_ws` for every `/ws/*` handler before `accept()`, else `await ws.close()` and return).
- Auth state: `auth.py` reads `app.state.session_key: str` and `app.state.allowed_origins: frozenset[str]` (from `allowed_origins(port, dev)`); `create_app` sets both, plus `app.state.settings`. New `/api` routers go in `create_app` above the SPA catch-all with `dependencies=[Depends(require_session)]`. Cookie name: `fenrir_session` (`SESSION_COOKIE`).
- Pure vs shell: `jobs.py` and the `auth.py` predicates are pure. `app.py` is glue around its pure helpers. Later: `jobs_runner.py` (`JobRunner`: `data/jobs/<id>.json`, worker thread, WebSocket listeners; plan 14), `fenrir serve` and `session.key` (11.3/11.5), PTY and routes (plans 13–16).
- Not here: running a backtest or the grid itself (`src/fenrir/backtest/`), rule logic (`src/fenrir/rules/`), the web UI (`web/`).
- Tests: `tests/server/`.
