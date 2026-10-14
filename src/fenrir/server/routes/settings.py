"""Settings API (contracts: Plan 24): the secrets file, the per-account choices, and the restart
that applies the secrets file in the image (#262)."""

import asyncio
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

import yaml
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import State

from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.live.arming import list_armed
from fenrir.rules.loader import list_accounts
from fenrir.server.isolation import in_image, nested_git_in_image
from fenrir.server.jobs import unfinished
from fenrir.settings import (
    Settings,
    env_file_path,
    mask_secret,
    parse_env_text,
    parse_settings,
    update_env_text,
)

router = APIRouter(prefix="/api/settings")

# the only keys the page edits: field -> env key. Paths and the private dir stay out of reach.
ENV_KEYS = {
    "lse_api_key": "FENRIR_LSE_API_KEY",
    "nt_url": "FENRIR_NT_URL",
    "nt_token": "FENRIR_NT_TOKEN",
    "holdout_months": "FENRIR_HOLDOUT_MONTHS",
    "safety_margin_pct": "FENRIR_SAFETY_MARGIN_PCT",
    "stale_seconds": "FENRIR_STALE_SECONDS",
}
SECRETS = ("lse_api_key", "nt_token")
MASK = "••••"


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    lse_api_key: str = ""
    nt_url: str = Field("", pattern=r"^https?://\S+$")
    nt_token: str = ""
    holdout_months: int = Field(0, ge=0)
    safety_margin_pct: float = Field(80.0, gt=0, le=100)
    stale_seconds: int = Field(180, gt=0)


def view(env: Mapping[str, str]) -> dict:
    """The page's settings from the secrets file's pairs, secrets masked; ValueError if bad."""
    s = parse_settings(env)
    shown = {f: getattr(s, f) for f in ENV_KEYS}
    return {**shown, **{f: mask_secret(shown[f]) for f in SECRETS}}


def env_updates(update: Mapping[str, object], env: Mapping[str, str]) -> tuple[dict, list[dict]]:
    """(env key -> new value, errors). A secret equal to its current mask is unchanged."""
    out, errors = {}, []
    for field, value in update.items():
        if field in SECRETS:
            if value == mask_secret(env.get(ENV_KEYS[field], "")):
                continue
            if str(value).startswith(MASK):  # a stale mask must never be saved as the secret
                errors.append({"loc": [field], "msg": "is a mask, not a secret; reload the page"})
                continue
        out[ENV_KEYS[field]] = str(value)
    return out, errors


def restart_refusals(
    image: bool, restarting: bool, armed: Sequence[str], pulling: bool, backtesting: bool
) -> list[str]:
    """Why `POST /api/settings/restart` must not restart now; empty means go."""
    if not image:  # nothing would start it again
        return ["Restart the server yourself: it runs natively, not in the Docker image."]
    if restarting:
        return ["The server is already restarting."]
    out = []
    if armed:
        out.append(f"Disarm every account first (armed: {', '.join(armed)}).")
    if pulling:
        out.append("A data pull is running; wait for it to finish.")
    if backtesting:
        out.append("A backtest job is running; wait for it to finish.")
    return out


def _invalid(detail: str, errors: list[dict]) -> JSONResponse:
    return JSONResponse({"detail": detail, "errors": errors}, status_code=422)


def _errors(e: ValidationError, at: tuple = ()) -> list[dict]:
    return [{"loc": [*at, *err["loc"]], "msg": err["msg"]} for err in e.errors()]


async def _body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError:
        return None


def _write(path: Path, data: bytes, mode: int) -> None:
    # temp + rename in the same dir: never half a file, and a symlink at `path` is replaced,
    # never written through
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _env_file() -> Path | None:
    return env_file_path(os.environ)


def _read_env(path: Path | None) -> str:
    try:
        # bytes, not read_text: keep \r\n lines byte for byte
        return path.read_bytes().decode("utf-8") if path else ""
    except FileNotFoundError:
        return ""


def _settings_view(text: str, nested: list[str], pending: bool) -> dict:
    try:
        shown = view(parse_env_text(text))
    except ValueError as e:  # a bad value typed into the file by hand: name it
        raise HTTPException(status_code=500, detail=f"{_env_file()}: {e}") from e
    return {**shown, "nested_git": nested, "can_restart": in_image(), "restart_pending": pending}


@router.get("")
def get_settings(request: Request) -> dict:
    nested = nested_git_in_image()  # in the threadpool
    return _settings_view(_read_env(_env_file()), nested, request.app.state.settings_saved)


@router.put("")
async def put_settings(request: Request):
    data = await _body(request)
    if not isinstance(data, dict):
        return _invalid(
            "Settings must be an object.", [{"loc": [], "msg": "body must be a JSON object"}]
        )
    # the walk off the event loop, before _save's no-await section
    return _save(request.app.state, data, await asyncio.to_thread(nested_git_in_image))


def _armed(settings: Settings) -> list[str]:
    """The armed accounts, read under sqlite's write lock: an arm in flight commits first (and is
    seen here) or takes the lock after `restarting` is set, and refuses."""
    path = settings.private_dir / "fenrir.db"
    if not path.exists():  # an arm creates the file before it checks `restarting`
        return []  # nothing was ever armed
    try:
        # not journal.open_db: that would create a missing `armed` table rather than refuse
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return [a.account for a in list_armed(conn)]  # the close rolls back
    except sqlite3.Error as e:  # fail closed: an unreadable `armed` never restarts
        raise HTTPException(status_code=500, detail=f"cannot read the armed accounts: {e}") from e


@router.post("/restart")
async def restart(request: Request, background: BackgroundTasks) -> Response:
    state = request.app.state
    if why := restart_refusals(in_image(), state.restarting, [], False, False):
        raise HTTPException(status_code=409, detail=" ".join(why))
    # set before the reads, with no await since the check: arm, pull and job submit refuse from
    # here on, and a second POST is "already restarting"
    state.restarting = True
    try:
        armed = await asyncio.to_thread(_armed, state.settings)
        why = restart_refusals(
            True,
            False,
            armed,
            state.pull.status()["state"] == "running",
            any(unfinished(j) for j in state.jobs.jobs()),
        )
    except BaseException:
        state.restarting = False
        raise
    if why:
        state.restarting = False
        raise HTTPException(status_code=409, detail=" ".join(why))
    # runs after the response is sent; uvicorn's SIGTERM shutdown runs the lifespan's stop()
    background.add_task(state.terminate)
    return Response(status_code=202)


def _save(state: State, data: dict, nested: list[str]):
    try:
        update = SettingsUpdate.model_validate(data).model_dump(exclude_unset=True)
    except ValidationError as e:
        return _invalid("The settings do not validate.", _errors(e))
    path = _env_file()
    if path is None:
        raise HTTPException(status_code=500, detail="no secrets file: FENRIR_ENV_FILE is not set")
    # no await from here on: read, merge and write cannot interleave with another save
    text = old = _read_env(path)
    updates, errors = env_updates(update, parse_env_text(text))
    if errors:
        return _invalid("The settings do not validate.", errors)
    if updates:
        try:
            text = update_env_text(text, updates)
        except ValueError as e:  # a value with a line break
            return _invalid("The settings do not validate.", [{"loc": [], "msg": str(e)}])
    changed = _parsed(text) != _parsed(old)  # a value written as it was changes nothing
    # built first: a 500 here must not follow a write
    response = _settings_view(text, nested, state.settings_saved or changed)
    if updates:
        _write(path, text.encode("utf-8"), 0o600)
        # the running server still has the old values until it restarts
        state.settings_saved = state.settings_saved or changed
    return response


def _parsed(text: str) -> Settings | None:
    try:
        return parse_settings(parse_env_text(text))
    except ValueError:  # a bad value typed into the file by hand
        return None


def _choices(data_dir: Path) -> dict[str, AccountChoice]:
    try:
        return load_account_choices(data_dir)
    except ValueError as e:  # a broken accounts.yaml: say which file, never overwrite it
        raise HTTPException(status_code=500, detail=str(e)) from e


def _accounts(rules_dir: Path) -> tuple[str, ...]:
    try:
        return list_accounts(rules_dir)
    except ValueError as e:  # a symlinked rules/ or firm dir: the message names it
        raise HTTPException(status_code=500, detail=str(e)) from e


def _accounts_view(rules_dir: Path, choices: Mapping[str, AccountChoice]) -> dict:
    return {aid: choices.get(aid, AccountChoice()).model_dump() for aid in _accounts(rules_dir)}


@router.get("/accounts")
def get_accounts(request: Request) -> dict:
    s = request.app.state.settings
    return _accounts_view(s.rules_dir, _choices(s.data_dir))


@router.put("/accounts")
async def put_accounts(request: Request):
    s = request.app.state.settings
    data = await _body(request)
    if not isinstance(data, dict):
        return _invalid(
            "Accounts must be an object.", [{"loc": [], "msg": "body must be a JSON object"}]
        )
    known = set(_accounts(s.rules_dir))
    current = _choices(s.data_dir)
    merged, errors = dict(current), []
    for aid, patch in data.items():
        if aid not in known:
            errors.append({"loc": [aid], "msg": "unknown account"})
        elif not isinstance(patch, dict):
            errors.append({"loc": [aid], "msg": "must be an object"})
        else:
            try:
                old = current.get(aid, AccountChoice()).model_dump()
                merged[aid] = AccountChoice.model_validate({**old, **patch})
            except ValidationError as e:
                errors += _errors(e, (aid,))
    if errors:
        return _invalid("The account choices do not validate.", errors)
    # shortcut: checked, not opened with O_NOFOLLOW (as jobs_runner does); upgrade with it
    if s.data_dir.is_symlink():
        raise HTTPException(status_code=500, detail=f"{s.data_dir} is a symlink; not writing")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    out = {aid: c.model_dump() for aid, c in sorted(merged.items())}
    # 0644: job children run as the terminal user and read dll_on from this file
    _write(s.data_dir / "accounts.yaml", yaml.safe_dump(out).encode("utf-8"), 0o644)
    return _accounts_view(s.rules_dir, merged)
