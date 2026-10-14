"""Backtests API (contracts: Plan 14): options, jobs, results, compare, and `/ws/jobs`.

Strategy files are never executed here (DEC-1): `/options` lists file names only, and a job's
grid is expanded by `app.state.jobs.expand` in an isolated child.
"""

import asyncio
import glob
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel

from fenrir.backtest.cache import list_reports, read_report
from fenrir.backtest.compare import View, compare, latest_only, summarize, with_net
from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.core.instruments import load_instruments
from fenrir.files import refuse_symlink
from fenrir.rules.loader import list_accounts
from fenrir.server.auth import check_ws
from fenrir.server.jobs import KEY, to_json
from fenrir.settings import Settings

router = APIRouter(prefix="/api/backtests")
ws_router = APIRouter()
_expand_slot = threading.Semaphore(1)  # one expand child at a time: each holds a thread


def cwd_relative(path: Path) -> str:
    """`path` relative to the cwd when it lies under it, else as given (absolute or not)."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:  # already relative, or outside the cwd
        return str(path)


def strategy_glob_ok(pattern: str, strategies_dir: Path) -> bool:
    """A glob with no `..` that resolves (relative ones against the cwd) inside `strategies_dir`.

    A first filter only: resolving a pattern cannot follow a symlink behind a wildcard part
    (`strategies/*.py` with `strategies/link.py -> /elsewhere`), so the `--expand` child
    resolves every match and refuses the grid, executing nothing, if any match is not a
    regular file inside `strategies_dir` (#153).
    """
    path = Path(pattern)
    return (
        "\x00" not in pattern  # resolve() raises on NUL
        and ".." not in path.parts
        and path.resolve().is_relative_to(strategies_dir.resolve())
    )


class JobRequest(BaseModel):
    strategies: list[str]
    accounts: list[str]
    contracts: list[str] | None
    include_holdout: bool = True


def _choices(settings: Settings) -> dict[str, AccountChoice]:
    try:
        return load_account_choices(settings.data_dir)
    except ValueError as e:  # a broken accounts.yaml: say which file, not a bare 500
        raise HTTPException(status_code=500, detail=str(e)) from e


def _reports(settings: Settings, limit: int | None = None) -> tuple[dict, ...]:
    try:
        return list_reports(settings, limit)
    except ValueError as e:  # a corrupt cached report: the message names the file to delete
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/options")
def options(request: Request) -> dict[str, list[str]]:
    settings = request.app.state.settings
    try:
        refuse_symlink(settings.strategies_dir)  # the terminal can swap it for a link (DEC-1)
        accounts = list_accounts(settings.rules_dir)  # refuses a symlinked rules/ or firm dir
        contracts = [s.symbol for s in load_instruments().specs]
    except ValueError as e:  # a link, or a planted instruments.yaml: the message names it
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        # file names only: reading MARKET would execute the file (DEC-1); escaped, as POST
        # takes globs
        "strategies": sorted(
            glob.escape(cwd_relative(p)) for p in settings.strategies_dir.glob("*.py")
        ),
        "accounts": accounts,
        "contracts": contracts,
    }


@router.post("/jobs")
def submit_job(body: JobRequest, request: Request) -> dict[str, str]:
    # sync handler, so the threadpool waits out `expand` (up to 60 s), not the event loop
    if request.app.state.restarting:  # POST /api/settings/restart (#262)
        raise HTTPException(status_code=409, detail="the server is restarting")
    settings = request.app.state.settings
    for pattern in body.strategies:
        if not strategy_glob_ok(pattern, settings.strategies_dir):
            raise HTTPException(
                status_code=422,
                detail=f"{pattern!r} is not a path inside {settings.strategies_dir}",
            )
    runner = request.app.state.jobs
    if not _expand_slot.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a grid is already being expanded")
    try:
        grid = runner.expand(body.strategies, body.accounts, body.contracts)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    finally:
        _expand_slot.release()
    return {"job_id": runner.submit(grid, body.include_holdout).id}


@router.get("/jobs")
def list_jobs(request: Request) -> list[dict[str, Any]]:
    return [to_json(j) for j in request.app.state.jobs.jobs()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    return to_json(job)


@router.get("/results")
def results(request: Request, limit: int | None = Query(None, ge=1, le=100)) -> list[dict]:
    settings = request.app.state.settings
    choices = _choices(settings)
    return [with_net(summarize(r), choices) for r in _reports(settings, limit)]


@router.get("/results/{key}")
def result(key: str, request: Request) -> dict:
    settings = request.app.state.settings
    if not KEY.fullmatch(key):  # the key becomes a file name
        raise HTTPException(status_code=404, detail=f"no report {key}")
    try:
        report = read_report(settings, key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no report {key}") from None
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return with_net(report, _choices(settings))


@router.get("/compare")
def compare_reports(view: View, request: Request) -> list[dict]:
    settings = request.app.state.settings
    choices = _choices(settings)
    return compare(latest_only(_reports(settings), choices), view, choices)


@ws_router.websocket("/ws/jobs")
async def jobs_ws(ws: WebSocket) -> None:
    """Every current `Job`, then each `Job` again on every change.

    Only the latest unsent payload per job id is kept, so a slow client costs at most one
    payload per job and skips the stale ones.
    """
    if not check_ws(ws):
        await ws.close()
        return
    await ws.accept()
    runner = ws.app.state.jobs
    loop = asyncio.get_running_loop()
    unsent: dict[str, dict[str, Any]] = {}
    ready = asyncio.Event()

    def put(job: dict[str, Any]) -> None:
        unsent[job["id"]] = job
        ready.set()

    def listener(job: dict[str, Any]) -> None:  # called on the runner or a submit thread
        loop.call_soon_threadsafe(put, job)

    runner.add_listener(listener)
    # snapshot after add_listener: a later change is scheduled after it, so the newest wins
    for job in runner.jobs():
        put(to_json(job))

    async def push() -> None:
        while True:
            await ready.wait()
            ready.clear()
            batch = list(unsent.values())
            unsent.clear()
            for job in batch:
                await ws.send_json(job)

    pusher = asyncio.create_task(push())
    try:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass  # the client sends nothing; reading is how a closed tab is noticed
    finally:
        runner.remove_listener(listener)  # before any await: a cancelled scope cancels it again
        pusher.cancel()
        await asyncio.gather(pusher, return_exceptions=True)
