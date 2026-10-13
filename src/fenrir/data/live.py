import asyncio
import logging
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from fenrir.core.bars import Bar
from fenrir.settings import Settings

MINUTE = timedelta(minutes=1)
# a minute closes this long after it ends on the wall clock, so ticks still in flight count
# (gate item 3, 2026-10-13: lag median 0.25 s, p95 0.55 s)
LAG_GRACE = timedelta(seconds=2)
# a tick stamped further ahead of the wall clock is dropped: it would open a future bar and
# every real tick before it would then be dropped as late
MAX_AHEAD = timedelta(seconds=5)

log = logging.getLogger(__name__)


class FeedStopped(Exception):
    """The live feed's client stopped and will not reconnect by itself: a refused key, quota, or
    the key's one socket still held (by another client, or a half-open old connection that the
    server drops later). Worth retrying with a backoff, except when `permanent` (no key in the
    settings, which only a restart changes)."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime  # trade time, tz-aware
    price: float
    size: int
    contract: str
    replay: bool = False  # replayed history (stream `start`, #79), not a trade just made


@dataclass(frozen=True, slots=True)
class BuilderState:
    bars: tuple[Bar, ...] = ()  # the open (partial) bar per contract
    closed_until: datetime | None = None  # latest close_due time: minutes ended by then are final


def empty_builder() -> BuilderState:
    return BuilderState()


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"naive datetime: {ts!r}")
    return ts


def feed(state: BuilderState, tick: Tick) -> tuple[BuilderState, tuple[Bar, ...]]:
    _aware(tick.ts)
    minute = tick.ts.astimezone(UTC).replace(second=0, microsecond=0)
    current = next((b for b in state.bars if b.contract == tick.contract), None)
    others = tuple(b for b in state.bars if b.contract != tick.contract)
    late = state.closed_until is not None and minute + MINUTE <= state.closed_until
    if late or (current is not None and minute < current.ts):
        return state, ()  # its bar is already emitted; a late tick cannot change it
    if current is not None and minute == current.ts:
        updated = replace(
            current,
            high=max(current.high, tick.price),
            low=min(current.low, tick.price),
            close=tick.price,
            volume=current.volume + tick.size,
        )
        return replace(state, bars=(*others, updated)), ()
    opened = Bar(minute, tick.price, tick.price, tick.price, tick.price, tick.size, tick.contract)
    closed = () if current is None else (current,)
    return replace(state, bars=(*others, opened)), closed


def close_due(state: BuilderState, now: datetime) -> tuple[BuilderState, tuple[Bar, ...]]:
    # ticks for minutes ended by `now` are dropped afterwards: the stream must pass
    # wall clock minus the feed's lag grace (data-gate item 3), not the raw wall clock
    _aware(now)
    until = now if state.closed_until is None else max(now, state.closed_until)
    due = tuple(b for b in state.bars if b.ts + MINUTE <= now)
    kept = tuple(b for b in state.bars if b.ts + MINUTE > now)
    return BuilderState(kept, until), due


def _now() -> datetime:
    return datetime.now(UTC)


def _handover(
    state: BuilderState, replayed: BuilderState, tick: Tick
) -> tuple[BuilderState, BuilderState, tuple[Bar, ...]]:
    """The first live tick after a replay of its contract: the replayed bar still open is
    complete when the tick is in a later minute, else the live builder carries it on."""
    c = tick.contract
    open_bar = next((b for b in replayed.bars if b.contract == c), None)
    if open_bar is None:
        return state, replayed, ()
    replayed = replace(replayed, bars=tuple(b for b in replayed.bars if b.contract != c))
    # a live bar from before the replay ends first (in practice closed on the clock long since)
    older = tuple(b for b in state.bars if b.contract == c and b.ts < open_bar.ts)
    state = replace(state, bars=tuple(b for b in state.bars if b not in older))
    minute = tick.ts.astimezone(UTC).replace(second=0, microsecond=0)
    if open_bar.ts < minute:
        return state, replayed, (*older, open_bar)
    others = tuple(b for b in state.bars if b.contract != c)
    return replace(state, bars=(*others, open_bar)), replayed, older


async def _next(ticks: AsyncIterator[Tick]) -> Tick | None:
    try:
        return await anext(ticks)
    except StopAsyncIteration:
        return None


async def _bars_from(
    ticks: AsyncGenerator[Tick, None],
    now: Callable[[], datetime],
    poll: float = 1.0,
    on_live: Callable[[str], None] | None = None,
) -> AsyncIterator[Bar]:
    """Closed bars from `ticks`, closing quiet minutes on a `poll`-second timer. `now` is the
    wall clock. When `ticks` ends, only bars already due are emitted, never a partial one.

    Replayed ticks (#79) build their own bars, each closed by the next minute's tick, never by
    the wall clock, until the contract's first live tick takes over. A bar at or before the last
    one emitted for its contract is never emitted again (a reconnect replays the day from the
    same start). `on_live(contract)` is called on each live tick, before the bars it closes."""
    state = empty_builder()
    replayed = empty_builder()  # never close_due: a replayed minute ends at a later tick
    last: dict[str, datetime] = {}  # the last bar emitted per contract
    dropped: Counter[str] = Counter()  # since the last warning: a clock off shows up here
    reported = now()

    def fresh(bars: tuple[Bar, ...]) -> list[Bar]:
        out = [b for b in bars if b.contract not in last or b.ts > last[b.contract]]
        last.update((b.contract, b.ts) for b in out)
        return out

    def report() -> None:
        counts = ", ".join(f"{n} {why}" for why, n in sorted(dropped.items()) if n)
        log.warning("live ticks dropped in the last minute: %s", counts)
        dropped.clear()

    pending = asyncio.ensure_future(_next(ticks))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=poll)
            bars: tuple[Bar, ...] = ()
            if done:
                tick = pending.result()
                if tick is None:
                    for bar in fresh(close_due(state, now() - LAG_GRACE)[1]):
                        yield bar
                    return
                if tick.replay:
                    replayed, bars = feed(replayed, tick)
                elif tick.ts > now() + MAX_AHEAD:
                    dropped["ahead of the clock"] += 1
                else:
                    if on_live is not None:
                        on_live(tick.contract)
                    state, replayed, bars = _handover(state, replayed, tick)
                    fed, more = feed(state, tick)
                    dropped["late"] += fed is state  # feed hands back the same state when it drops
                    state, bars = fed, bars + more
                pending = asyncio.ensure_future(_next(ticks))
            state, due = close_due(state, now() - LAG_GRACE)
            for bar in fresh(bars + due):
                yield bar
            if +dropped and now() - reported >= MINUTE:
                report()
                reported = now()
    finally:
        if +dropped:  # the stream ended or was closed: still say what was dropped
            report()
        pending.cancel()
        await asyncio.wait({pending})  # let the source see the cancel, then close it
        await ticks.aclose()


async def stream_bars(
    settings: Settings,
    contracts: Sequence[str],
    *,
    on_auth: Callable[[], None] | None = None,
    start: datetime | None = None,
    on_live: Callable[[str], None] | None = None,
) -> AsyncIterator[Bar]:
    """Closed 1-minute bars for `contracts` (front-month codes) from the LSE stream. The SDK
    reconnects after a drop; `FeedStopped` when it stops instead (no or bad key, quota, or the
    key's one socket still held). `on_auth` is called each time the socket authenticates. With
    `start`, each (re)subscription first replays the ticks since then (#79): their bars come
    first, and `on_live(contract)` on each live tick says the replay of that contract is over.
    Closing the stream closes the socket."""
    from fenrir.data.lse import stream_ticks  # lse imports Tick from here

    ticks = stream_ticks(settings, contracts, on_auth=on_auth, start=start)
    async with aclosing(_bars_from(ticks, _now, on_live=on_live)) as bars:
        async for bar in bars:
            yield bar
