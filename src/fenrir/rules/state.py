"""Account state, events and breaches shared by every rule function. Pure values only."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Phase = Literal["evaluation", "funded"]


@dataclass(frozen=True, slots=True)
class AccountState:
    phase: Phase
    day: date  # current trading day
    balance: float  # realized P&L net of commissions since phase start
    open_pnl: float  # unrealized P&L at last mark (close)
    position: int  # signed net contracts
    peak: float  # high-water mark the trailing drawdown follows
    floor: float  # equity at or below this = breach
    day_start_balance: float
    closed_days: tuple[tuple[date, float], ...]  # (day, net realized P&L) per day with >= 1 fill
    traded_today: bool
    halted_today: bool  # daily loss limit hit: no new entries until DayClosed (not a breach)
    possible_microscalp_pnl: float  # profit from trades opened and closed in the same bar


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    qty: int  # signed: + buy, - sell
    price: float
    commission: float
    realized_pnl: float  # gross $ realized by this fill (0 for opening fills)


@dataclass(frozen=True, slots=True)
class Mark:
    ts: datetime  # bar open time
    worst_open_pnl: float  # unrealized P&L at the bar's adverse extreme
    best_open_pnl: float  # unrealized P&L at the bar's favorable extreme
    close_open_pnl: float


@dataclass(frozen=True, slots=True)
class TradeClosed:
    opened: datetime
    closed: datetime
    pnl: float
    same_bar: bool


@dataclass(frozen=True, slots=True)
class DayClosed:
    day: date
    ts: datetime


Event = Fill | Mark | TradeClosed | DayClosed


@dataclass(frozen=True, slots=True)
class Breach:
    rule: str  # "max_drawdown" | "flat_by"
    reason: str
    ts: datetime


Rule = Callable[[AccountState, Event], AccountState | Breach]


@dataclass(frozen=True, slots=True)
class AccountView:
    phase: Phase
    drawdown_remaining: float  # equity - floor
    daily_loss_remaining: float | None
    today_pnl: float  # realized today + open
    target_remaining: float | None  # None in funded phase
