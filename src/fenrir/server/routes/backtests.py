"""Backtests API (contracts: Plan 14): options, jobs, results, compare, and `/ws/jobs`.

Strategy files are never executed here (DEC-1): `/options` lists file names only, and a job's
grid is expanded by `app.state.jobs.expand` in an isolated child.
"""

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel

from fenrir.backtest.cache import list_reports, read_report
from fenrir.backtest.compare import View, compare, latest_only, summarize, with_net
from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.core.instruments import load_instruments
from fenrir.rules.loader import list_accounts
from fenrir.server.auth import check_ws
from fenrir.server.jobs import KEY, to_json
from fenrir.settings import Settings

router = APIRouter(prefix="/api/backtests")
ws_router = APIRouter()


def strategy_glob_ok(pattern: str, strategies_dir: Path) -> bool:
    """A cwd-relative glob with no `..` that resolves inside `strategies_dir`.

    Checked before `expand`, whose child executes every file the glob matches.
    """
    path = Path(pattern)
    return (
        "\x00" not in pattern  # resolve() raises on NUL
        and not path.is_absolute()
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


def _reports(settings: Settings) -> tuple[dict, ...]:
    try:
        return list_reports(settings)
    except ValueError as e:  # a corrupt cached report: the message names the file to delete
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/options")
def options(request: Request) -> dict[str, list[str]]:
    settings = request.app.state.settings
    return {
        # file names only: reading MARKET would execute the file (DEC-1)
        "strategies": sorted(str(p) for p in settings.strategies_dir.glob("*.py")),
        "accounts": list_accounts(settings.rules_dir),
        "contracts": [s.symbol for s in load_instruments().specs],
    }


@router.post("/jobs")
def submit_job(body: JobRequest, request: Request) -> dict[str, str]:
    # sync handler, so the threadpool waits out `expand` (up to 60 s), not the event loop
    settings = request.app.state.settings
    for pattern in body.strategies:
        if not strategy_glob_ok(pattern, settings.strategies_dir):
            raise HTTPException(
                status_code=422,
                detail=f"{pattern!r} is not a relative path inside {settings.strategies_dir}",
            )
    runner = request.app.state.jobs
    try:
        grid = runner.expand(body.strategies, body.accounts, body.contracts)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
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
def results(request: Request) -> list[dict]:
    settings = request.app.state.settings
    choices = _choices(settings)
    return [with_net(summarize(r), choices) for r in _reports(settings)]


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
    """Every current `Job`, then each `Job` again on every change."""
    if not check_ws(ws):
        await ws.close()
        return
    await ws.accept()
    runner = ws.app.state.jobs
    loop = asyncio.get_running_loop()
    changes: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def listener(job: dict[str, Any]) -> None:  # called on the runner or a submit thread
        loop.call_soon_threadsafe(changes.put_nowait, job)

    runner.add_listener(listener)
    # snapshot after add_listener: a change in between is queued too, so none is lost
    current = [to_json(j) for j in runner.jobs()]

    async def push() -> None:
        for job in current:
            await ws.send_json(job)
        while True:
            await ws.send_json(await changes.get())

    pusher = asyncio.create_task(push())
    try:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass  # the client sends nothing; reading is how a closed tab is noticed
    finally:
        runner.remove_listener(listener)  # before any await: a cancelled scope cancels it again
        pusher.cancel()
        await asyncio.gather(pusher, return_exceptions=True)
