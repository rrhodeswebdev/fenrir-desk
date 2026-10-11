"""Rules API (contracts: Plan 15): list accounts, read one as JSON, save a form edit."""

import os
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from fenrir.rules.loader import account_id, list_accounts, load_account, rules_path
from fenrir.rules.schema import AccountRules
from fenrir.rules.writer import write_account

STALE_AFTER_DAYS = 30

router = APIRouter(prefix="/api/rules")


def staleness(verified_on: date | None, today: date) -> dict:
    if verified_on is None:  # never checked against the firm's site
        return {"verified_on": None, "age_days": None, "stale": True}
    age = (today - verified_on).days
    return {
        "verified_on": verified_on.isoformat(),
        "age_days": age,
        "stale": age < 0 or age > STALE_AFTER_DAYS,  # a future date is a typo, not fresh
    }


def _invalid(detail: str, errors: list[dict]) -> JSONResponse:
    return JSONResponse({"detail": detail, "errors": errors}, status_code=422)


def _relative(e: ValueError, rules_dir: Path) -> str:
    """The loader's message with paths relative to `rules_dir`, so the UI shows `lucid/x.yaml`."""
    return str(e).replace(f"{rules_dir}{os.sep}", "")


def _load(aid: str, rules_dir: Path) -> tuple[dict, AccountRules | None]:
    """The list row for `aid` and its rules, or None when the file does not validate."""
    try:
        rules = load_account(aid, rules_dir)
    except ValueError as e:
        return {
            "id": aid,
            "error": _relative(e, rules_dir),
            "verified_on": None,
            "age_days": None,
            "stale": None,
        }, None
    return {"id": aid, "error": None, **staleness(rules.verified_on, date.today())}, rules


def _rules_dir(aid: str, request: Request) -> Path:
    rules_dir = request.app.state.settings.rules_dir
    if aid not in list_accounts(rules_dir):  # only existing ids reach the disk
        raise HTTPException(status_code=404, detail=f"unknown account {aid}")
    return rules_dir


@router.get("")
def list_rules(request: Request) -> list[dict]:
    rules_dir = request.app.state.settings.rules_dir
    return [_load(aid, rules_dir)[0] for aid in list_accounts(rules_dir)]


@router.get("/{aid}")
def get_rules(aid: str, request: Request) -> dict:
    row, rules = _load(aid, _rules_dir(aid, request))
    return {**row, "rules": None if rules is None else rules.model_dump(mode="json")}


@router.put("/{aid}")
async def save_rules(aid: str, request: Request):
    rules_dir = _rules_dir(aid, request)
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return _invalid(
            "The rules must be an object.", [{"loc": [], "msg": "body must be a JSON object"}]
        )
    try:
        rules = AccountRules.model_validate(data)
    except ValidationError as e:
        errors = [{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()]
        return _invalid("The rules do not validate.", errors)
    if account_id(rules) != aid:
        msg = f"firm and account give {account_id(rules)}, not {aid}; renaming is not supported"
        return _invalid("The rules do not validate.", [{"loc": ["account"], "msg": msg}])
    try:
        # inline in an async handler on purpose: saves run one at a time, never sharing the tmp file
        write_account(rules_path(aid, rules_dir), rules)
    except ValueError as e:  # the file on disk is broken, or the edit would not read back
        return _invalid(_relative(e, rules_dir), [])
    return {"id": aid, "error": None, **staleness(rules.verified_on, date.today())}
