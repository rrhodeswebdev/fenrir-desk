"""The only module that knows about London Strategic Edge (docs/data-gate.md "Client API")."""

import logging
import math
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from datetime import UTC, datetime, time, timedelta

import polars as pl

from fenrir.core.sessions import NY
from fenrir.data.live import FeedStopped, Tick
from fenrir.data.pull import Window
from fenrir.data.store import SCHEMA
from fenrir.settings import Settings

PAGE = 5000  # max rows per candles() call
TICK = 0.25  # ES and NQ price increment

log = logging.getLogger(__name__)


def lse_symbol(contract: str) -> str:
    # LSE has continuous series only ("NQ.F"): unadjusted, on LSE's own roll dates
    return contract[:-3] + ".F"


# never let the SDK fall back to its LSE_API_KEY env var: the settings decide the key
NO_KEY = "FENRIR_LSE_API_KEY is not set; add the LSE key on the Settings page"


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")  # naive strings are read as UTC


def fetch_contract(settings: Settings, window: Window) -> pl.DataFrame:
    from lse import LSE

    if not settings.lse_api_key:
        raise ValueError(NO_KEY)
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


async def stream_ticks(
    settings: Settings,
    contracts: Sequence[str],
    *,
    on_auth: Callable[[], None] | None = None,
    start: datetime | None = None,
) -> AsyncGenerator[Tick, None]:
    """Live trades for our front-month `contracts`, from LSE's continuous `ES.F`/`NQ.F`
    (docs/data-gate.md Item 3). The SDK reconnects after a drop and stops only on an error it
    deems fatal (any error before auth), which raises `FeedStopped` with the server's last
    error message; an empty key raises a permanent `FeedStopped` before any connection.
    `on_auth` is called each time the socket authenticates (the first connect and each SDK
    reconnect), before any tick on it. With `start`, every (re)subscription first replays the
    trades since then (LSE: at most 24 h), flagged `replay` (#79)."""
    from lse import LSE, LSEError

    ours = {lse_symbol(c): c for c in contracts}
    if not settings.lse_api_key:
        raise FeedStopped(NO_KEY, permanent=True)
    client = LSE(api_key=settings.lse_api_key)
    errors: list[str] = []

    def on_error(message: str) -> None:
        errors.append(message)  # the server's text; it never carries the key
        log.warning("LSE stream: %s", message)

    client.on("error", on_error)
    if on_auth is not None:
        client.on("authenticated", on_auth)
    try:
        replay_from = start and _utc(start)
        async with aclosing(
            client.stream_async(list(ours), reconnect=True, start=replay_from)
        ) as ticks:
            async for t in ticks:
                ts, contract, price = t.datetime, ours.get(t.symbol), t.price
                # quotes (no volume), untimed or naive ticks and bad prices are not trades (a
                # missing price arrives as 0.0)
                if (
                    ts is None
                    or ts.tzinfo is None
                    or not t.volume
                    or contract is None
                    or price is None
                    or not math.isfinite(price)
                    or price <= 0
                ):
                    continue
                yield Tick(
                    ts.astimezone(UTC),
                    round(price / TICK) * TICK,
                    round(t.volume),
                    contract,
                    bool(t.replay),
                )
    except LSEError as e:  # lse-data 0.14 only raises it from REST calls; kept for later versions
        raise FeedStopped(f"LSE stream failed: {e}") from e
    # with reconnect=True the SDK returns only after a fatal error (or a disconnect we never ask)
    raise FeedStopped(f"LSE stream stopped: {errors[-1] if errors else 'no reason given'}")
