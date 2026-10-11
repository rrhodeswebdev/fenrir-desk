"""Conservative fill model (spec §6). Pure."""

from dataclasses import dataclass
from datetime import datetime

from fenrir.core.actions import Side
from fenrir.core.bars import Bar


@dataclass(frozen=True, slots=True)
class OpenPosition:
    qty: int  # signed contracts
    entry: float
    stop: float
    target: float
    opened: datetime  # ts of the bar the entry filled in


def entry_fill(side: Side, bar: Bar, tick: float) -> float:
    return bar.open + tick if side == "long" else bar.open - tick


def market_exit(qty: int, price: float, tick: float) -> float:
    return price - tick if qty > 0 else price + tick


def bracket_exit(pos: OpenPosition, bar: Bar, tick: float) -> float | None:
    """Exit price if the stop or target trades inside `bar`; the stop wins when both do."""
    if pos.qty > 0:
        if bar.low <= pos.stop:
            return min(pos.stop, bar.open) - tick
        if bar.high >= pos.target:
            return pos.target
    else:
        if bar.high >= pos.stop:
            return max(pos.stop, bar.open) + tick
        if bar.low <= pos.target:
            return pos.target
    return None


def gross_pnl(qty: int, entry: float, exit: float, point_value: float) -> float:
    return (exit - entry) * qty * point_value


def worst_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float:
    return gross_pnl(pos.qty, pos.entry, bar.low if pos.qty > 0 else bar.high, point_value)


def best_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float:
    return gross_pnl(pos.qty, pos.entry, bar.high if pos.qty > 0 else bar.low, point_value)
