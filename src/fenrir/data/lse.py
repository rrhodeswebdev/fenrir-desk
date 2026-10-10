"""The only module that knows about London Strategic Edge (docs/data-gate.md "Client API")."""

from datetime import UTC, datetime, time, timedelta

import polars as pl

from fenrir.core.sessions import NY
from fenrir.data.pull import Window
from fenrir.data.store import SCHEMA
from fenrir.settings import Settings

PAGE = 5000  # max rows per candles() call


def lse_symbol(contract: str) -> str:
    # LSE has continuous series only ("NQ.F"): unadjusted, on LSE's own roll dates
    return contract[:-3] + ".F"


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")  # naive strings are read as UTC


def fetch_contract(settings: Settings, window: Window) -> pl.DataFrame:
    from lse import LSE

    client = LSE(api_key=settings.lse_api_key)
    cursor = datetime.combine(window.first - timedelta(days=1), time(18), NY)  # session open
    end = datetime.combine(window.last, time(17), NY)  # session close
    rows: list[dict] = []
    while cursor < end:
        page = client.candles(
            lse_symbol(window.contract),
            "1m",
            start=_utc(cursor),
            end=_utc(end),
            limit=PAGE,
            order="asc",
            dataset="futures",
        )
        rows += page
        if len(page) < PAGE:
            break
        cursor = datetime.fromisoformat(page[-1]["timestamp"]) + timedelta(minutes=1)
    return pl.DataFrame(
        {
            # timestamps are bar open, UTC (verified by the gate)
            "ts": [datetime.fromisoformat(r["timestamp"]).astimezone(UTC) for r in rows],
            # the vault returns float noise (7827.250000000001); ES/NQ prices sit on 0.25 ticks
            **{c: [round(r[c], 2) for r in rows] for c in ("open", "high", "low", "close")},
            "volume": [round(r["volume"]) for r in rows],
        },
        schema=SCHEMA,
    )
