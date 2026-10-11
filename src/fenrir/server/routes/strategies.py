"""Strategies API (contracts: Plans 21–24): each file with its MARKET, PARAMS and the backtests
of its current contents.

Strategy files are never executed here (DEC-1): this lists file names only, and
`app.state.jobs.describe` loads them in an isolated child.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from fenrir.backtest.cache import list_reports
from fenrir.backtest.compare import summarize, with_net
from fenrir.backtest.run import load_account_choices

router = APIRouter(prefix="/api/strategies")


@router.get("")
def strategies(request: Request) -> list[dict[str, Any]]:
    # sync handler, so the threadpool waits out the describe child (up to 30 s), not the loop
    settings = request.app.state.settings
    try:
        choices = load_account_choices(settings.data_dir)
        reports = list_reports(settings)
    except ValueError as e:  # a broken accounts.yaml or cached report: the message names it
        raise HTTPException(status_code=500, detail=str(e)) from e
    paths = sorted(str(p) for p in settings.strategies_dir.glob("*.py"))
    return [
        entry
        | {
            "reports": []
            if entry["error"]
            else [
                with_net(summarize(r), choices)
                for r in reports
                if r["strategy_hash"] == entry["hash"]
            ]
        }
        for entry in request.app.state.jobs.describe(paths)
    ]
