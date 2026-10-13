"""Arming API (contracts: Plans 21–24): arm and disarm a strategy on a NinjaTrader account.

UI-only: no CLI counterpart, and the terminal never has the session. Every rule is in
`fenrir.live.arming.decide`; this gathers its inputs and writes its answer. The strategy file is
hashed, never executed (DEC-1). The hub picks up a change to `armed` on its next tick.
"""

import hashlib
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from fenrir.backtest.cache import list_reports
from fenrir.backtest.run import load_account_choices
from fenrir.core.instruments import read_instruments
from fenrir.files import read_regular, refuse_symlink
from fenrir.live import journal
from fenrir.live.arming import (
    Armed,
    ArmRequest,
    Mode,
    Refusal,
    decide,
    insert_armed,
    list_armed,
)
from fenrir.live.arming import disarm as disarm_armed
from fenrir.rules.loader import read_account
from fenrir.rules.state import Phase
from fenrir.server.jobs_runner import STRATEGY_MAX
from fenrir.settings import Settings

router = APIRouter(prefix="/api/arming")


class ArmBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    account: str
    strategy_path: str
    rules_account: str
    contract: str
    mode: Mode
    phase: Phase
    confirm: bool = False
    # NinjaTrader balances at phase start and the peak so far (DEC-4a), pinned on `armed`
    start_balance: float | None = Field(None, gt=0)
    peak_balance: float | None = Field(None, gt=0)
    # a live confirm echoes these from the needs_confirmation summary (what the user saw)
    strategy_hash: str | None = None
    rules_hash: str | None = None
    dll_on: bool | None = None

    @model_validator(mode="after")
    def _balances(self) -> "ArmBody":
        if self.mode != "dry":
            if self.start_balance is None or self.peak_balance is None:
                raise ValueError("sim and live need start_balance and peak_balance")
            if self.peak_balance < self.start_balance:
                raise ValueError("peak_balance is below start_balance")
        return self


class DisarmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str


def _now() -> datetime:
    return datetime.now(UTC)


def _db(settings: Settings) -> Path:
    return settings.private_dir / "fenrir.db"


def _json(a: Armed) -> dict[str, Any]:
    return asdict(a) | {"armed_at": a.armed_at.isoformat()}  # "+00:00", as the journal API


def _refuse(error: str, status: int, summary: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "summary": summary}, status_code=status)


async def _nt_accounts(request: Request) -> list[dict[str, Any]] | None:
    """NinjaTrader's accounts, or None when the add-on is down or not configured."""
    addon = request.app.state.addon
    if addon is None:
        return None
    try:
        return await addon.accounts()
    except (httpx.HTTPError, ValueError):  # ValueError: a malformed reply
        return None


@router.get("")
def armed(request: Request) -> list[dict[str, Any]]:
    path = _db(request.app.state.settings)
    if not path.exists():
        return []  # nothing was ever armed
    with closing(journal.open_ro(path)) as conn:
        return [_json(a) for a in list_armed(conn)]


@router.get("/accounts", response_model=None)
async def accounts(request: Request) -> list[dict[str, Any]] | JSONResponse:
    rows = await _nt_accounts(request)
    return _refuse("nt_unavailable", 502) if rows is None else rows


@router.post("/arm", response_model=None)
async def arm(body: ArmBody, request: Request) -> dict[str, Any] | JSONResponse:
    settings = request.app.state.settings
    path = Path(body.strategy_path)
    if path.parent != settings.strategies_dir or path.suffix != ".py":
        raise HTTPException(400, f"{body.strategy_path!r} is not a .py file in strategies/")
    is_sim = None
    if body.mode != "dry":
        rows = await _nt_accounts(request)
        if rows is None:
            return _refuse("nt_unavailable", 502)
        is_sim = next((a["is_sim"] for a in rows if a["id"] == body.account), None)
    # files and sqlite in the threadpool, off the hub's loop
    return await run_in_threadpool(_arm, settings, body, path, is_sim)


def _arm(
    settings: Settings, body: ArmBody, path: Path, is_sim: bool | None
) -> dict[str, Any] | JSONResponse:
    try:
        refuse_symlink(settings.strategies_dir)  # the terminal can swap it for a link (DEC-1)
        digest = hashlib.sha256(read_regular(path, STRATEGY_MAX)).hexdigest()
        rules, rules_hash = read_account(body.rules_account, settings.rules_dir)
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    try:
        reports, choices = list_reports(settings), load_account_choices(settings.data_dir)
        _, instruments = read_instruments()  # its hash is pinned (DEC-4d)
    except (OSError, ValueError) as e:  # a broken cached report, accounts.yaml or instruments
        raise HTTPException(500, str(e)) from e
    req = ArmRequest(
        body.account,
        body.strategy_path,
        body.rules_account,
        body.contract,
        body.mode,
        body.phase,
        body.confirm,
        body.start_balance,
        body.peak_balance,
        body.strategy_hash,
        body.rules_hash,
        body.dll_on,
    )
    with closing(journal.open_db(_db(settings))) as conn:
        # sqlite's write lock across list -> decide -> insert: two arms never both pass
        # already_armed (insert_armed replaces); a refusal is rolled back by the close
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = decide(
                req,
                list_armed(conn),
                path.stem,
                digest,
                rules_hash,
                instruments,
                reports,
                is_sim,
                rules,
                choices,
                _now(),  # a fresh armed_at: halts and peaks are restored since it (#169)
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if isinstance(result, Refusal):
            return _refuse(result.error, 409, result.summary)
        insert_armed(conn, result)  # commits
    return _json(result)


@router.post("/disarm")
def disarm(body: DisarmBody, request: Request) -> dict[str, bool]:
    path = _db(request.app.state.settings)
    if not path.exists():
        return {"ok": False}
    with closing(journal.open_db(path)) as conn:
        # the "halt" row and the delete in one transaction (the hub restores halts, #169)
        return {"ok": disarm_armed(conn, body.account, _now())}
