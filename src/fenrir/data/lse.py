"""The only module that knows about London Strategic Edge (docs/data-gate.md "Client API")."""

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from datetime import UTC, datetime, time, timedelta

import polars as pl

from fenrir.core.sessions import NY
from fenrir.data.live import Tick
from fenrir.data.pull import Window
from fenrir.data.store import SCHEMA
from fenrir.settings import Settings

PAGE = 5000  # max rows per candles() call
TICK = 0.25  # ES and NQ price increment

log = logging.getLogger(__name__)


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
            # vault returns float noise and off-tick values; snap to 0.25 tick
            **{
                c: [round(r[c] / TICK) * TICK for r in rows]
                for c in ("open", "high", "low", "close")
            },
            "volume": [round(r["volume"]) for r in rows],
        },
        schema=SCHEMA,
    )


async def stream_ticks(settings: Settings, contracts: Sequence[str]) -> AsyncGenerator[Tick, None]:
    """Live trades for our front-month `contracts`, from LSE's continuous `ES.F`/`NQ.F`
    (docs/data-gate.md Item 3). Ends when the SDK stops: on a fatal error it logs and returns."""
    from lse import LSE, LSEError

    ours = {lse_symbol(c): c for c in contracts}
    client = LSE(api_key=settings.lse_api_key)
    client.on("error", lambda message: log.warning("LSE stream: %s", message))
    try:
        async with aclosing(client.stream_async(list(ours), reconnect=True)) as ticks:
            async for t in ticks:
                ts, contract = t.datetime, ours.get(t.symbol)
                # replayed history, quotes (no volume) and untimed ticks are not trades
                if t.replay or ts is None or not t.volume or contract is None:
                    continue
                yield Tick(
                    ts.astimezone(UTC), round(t.price / TICK) * TICK, round(t.volume), contract
                )
    except LSEError as e:  # lse-data 0.14 only raises it from REST calls; kept for later versions
        log.warning("LSE stream failed: %s", e)
