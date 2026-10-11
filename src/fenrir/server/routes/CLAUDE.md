# src/fenrir/server/routes/

One FastAPI router per `/api/<area>`, each `router = APIRouter(prefix="/api/<area>")`, registered in `create_app` with `dependencies=[Depends(require_session)]`. A WebSocket router (`/ws/<name>`) is registered without it and calls `check_ws` before `accept()`. Shapes in `docs/contracts.md`.

- Entry points: `rules.py` (Plan 15: list, read, save account rules; pure `staleness`); `data.py` (Plan 16: `GET /api/data`, `POST /api/data/pull`; pure `front_months(instruments, day)` → `{ES, NQ}`, None outside the rollover calendar); `terminal.py` (Plan 13: `WS /ws/terminal`, the Workbench shell; process in `../ptyproc.py`, protocol in `../terminal.py`).
- Pure vs shell: handlers only parse, check and forward; pure helpers sit at the top of each module. Shared state comes from `request.app.state` (`settings`, `pull`).
- Not here: auth and the app itself (`../`), long-running work (`../pull.py`), rule logic and file writes (`src/fenrir/rules/`), coverage (`src/fenrir/data/`).
- Tests: `tests/server/test_<area>_routes.py`; the terminal in `test_terminal_ws.py`.
