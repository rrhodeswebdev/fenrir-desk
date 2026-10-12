"""Data API: coverage and gaps per series, the rollover calendar and the background pull."""

from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from fenrir.core.instruments import Instruments, front_month, load_instruments
from fenrir.core.sessions import trading_day
from fenrir.data.store import SERIES, coverage

router = APIRouter(prefix="/api/data")


def front_months(instruments: Instruments, day: date) -> dict[str, str | None]:
    def one(series: str) -> str | None:
        try:
            return front_month(instruments, series, day)
        except ValueError:  # day outside the rollover calendar
            return None

    return {s: one(s) for s in SERIES}


@router.get("")
def overview(request: Request) -> dict[str, Any]:
    # the same coverage() as `fenrir data summary --json`, so the page and the CLI agree
    data_dir = request.app.state.settings.data_dir
    instruments = load_instruments()
    return {
        "series": [asdict(coverage(data_dir, s)) for s in SERIES],
        "rolls": [asdict(r) for r in instruments.rolls],
        "front": front_months(instruments, trading_day(datetime.now(UTC))),
        "pull": request.app.state.pull.status(),
    }


@router.post("/pull")
def pull(request: Request) -> Response:
    try:
        started = request.app.state.pull.start()
    except ValueError as e:  # the image fails the DEC-1 isolation check: a server fault, not yours
        raise HTTPException(status_code=500, detail=str(e)) from None
    if not started:
        raise HTTPException(status_code=409, detail="a pull is already running")
    return Response(status_code=202)
