from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from fenrir.core.bars import Bar

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Tick:
    ts: datetime  # trade time, tz-aware
    price: float
    size: int
    contract: str


@dataclass(frozen=True, slots=True)
class BuilderState:
    bars: tuple[Bar, ...] = ()  # the open (partial) bar per contract
    closed_until: datetime | None = None  # latest close_due time: minutes ended by then are final


def empty_builder() -> BuilderState:
    return BuilderState()


def feed(state: BuilderState, tick: Tick) -> tuple[BuilderState, tuple[Bar, ...]]:
    if tick.ts.tzinfo is None:
        raise ValueError(f"naive datetime: {tick.ts!r}")
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
    until = now if state.closed_until is None else max(now, state.closed_until)
    due = tuple(b for b in state.bars if b.ts + MINUTE <= now)
    kept = tuple(b for b in state.bars if b.ts + MINUTE > now)
    return BuilderState(kept, until), due
