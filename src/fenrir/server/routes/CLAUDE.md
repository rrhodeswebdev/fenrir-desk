# src/fenrir/server/routes/

One FastAPI router per `/api/<area>`, each `router = APIRouter(prefix="/api/<area>")`, registered in `create_app` with `dependencies=[Depends(require_session)]`. Shapes in `docs/contracts.md`.

- Entry points: `rules.py` (Plan 15: list, read, save account rules; pure `staleness`); `data.py` (Plan 16: `GET /api/data`, `POST /api/data/pull`; pure `front_months(instruments, day)` → `{ES, NQ}`, None outside the rollover calendar); `strategies.py` (Plan 23: `GET /api/strategies`; file names only here, `app.state.jobs.describe` loads them in the isolated child).
- Pure vs shell: handlers only parse, check and forward; pure helpers sit at the top of each module. Shared state comes from `request.app.state` (`settings`, `pull`, `jobs`).
- Not here: auth and the app itself (`../`), long-running work (`../pull.py`), rule logic and file writes (`src/fenrir/rules/`), coverage (`src/fenrir/data/`).
- Tests: `tests/server/test_<area>_routes.py`.
