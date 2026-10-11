# src/fenrir/server/routes/

One FastAPI router per `/api/<area>`, each `router = APIRouter(prefix="/api/<area>")`, registered in `create_app` with `dependencies=[Depends(require_session)]`. Shapes in `docs/contracts.md`.

- Entry points: `rules.py` (Plan 15: list, read, save account rules; pure `staleness`).
- Pure vs shell: handlers only parse, check and forward; pure helpers sit at the top of each module.
- Not here: auth and the app itself (`../`), rule logic and file writes (`src/fenrir/rules/`).
- Tests: `tests/server/test_<area>_routes.py`.
