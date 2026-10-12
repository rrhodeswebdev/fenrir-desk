"""Live API: the runner's status, from the hub's memory, and its journal."""

from contextlib import closing
from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Request

from fenrir.live import journal

router = APIRouter(prefix="/api/live")


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    # async, on the hub's loop: in memory, no database
    return request.app.state.live.status()


@router.get("/journal")
def journal_rows(request: Request, account: str, day: date) -> list[dict[str, Any]]:
    # sync, so in the threadpool, on its own read-only connection: never the hub's
    path = request.app.state.settings.private_dir / "fenrir.db"
    if not path.exists():
        return []  # nothing was ever armed
    with closing(journal.open_ro(path)) as conn:
        # ts as status writes it ("+00:00"), not pydantic's "Z"
        return [asdict(r) | {"ts": r.ts.isoformat()} for r in journal.rows(conn, account, day)]
