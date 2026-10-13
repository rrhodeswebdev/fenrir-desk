"""Live API: the runner's status, from the hub's memory, its journal, the kill switch and
Resolve. Kill and resolve are UI-only, like arming: no CLI counterpart."""

from contextlib import closing
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from fenrir.live import journal

router = APIRouter(prefix="/api/live")


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    # async, on the hub's loop: in memory, no database
    return request.app.state.live.status()


Kind = Literal["bar", "action", "rejection", "fill", "alert", "halt", "peak"]


@router.get("/journal")
def journal_rows(
    request: Request,
    day: date,
    account: str | None = None,
    skip: Annotated[list[Kind] | None, Query()] = None,
) -> list[dict[str, Any]]:
    """One trading day's rows, every account's unless `account` names one (so a disarmed account's
    rows still show); `skip` leaves kinds out, e.g. the per-minute "bar" rows for the Live tape."""
    # sync, so in the threadpool, on its own read-only connection: never the hub's
    path = request.app.state.settings.private_dir / "fenrir.db"
    if not path.exists():
        return []  # nothing was ever armed
    with closing(journal.open_ro(path)) as conn:
        # ts as status writes it ("+00:00"), not pydantic's "Z"
        return [
            asdict(r) | {"ts": r.ts.isoformat()}
            for r in journal.rows(conn, account, day)
            if r.kind not in (skip or ())
        ]


@router.post("/kill")
async def kill(request: Request) -> dict[str, Any]:
    # async, on the hub's loop: under its lock, as every bar and fill
    return await request.app.state.live.kill()


@router.post("/resolve")
async def resolve(body: ResolveBody, request: Request) -> dict[str, bool]:
    try:
        return {"ok": await request.app.state.live.resolve(body.account)}
    except KeyError:
        raise HTTPException(404, f"{body.account!r} is not running") from None
