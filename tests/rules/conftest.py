import copy
from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from fenrir.rules.state import AccountState

BASE = {
    "firm": "lucid",
    "account": "flex-50k",
    "verified_on": None,
    "source_url": "https://lucidtrading.com",
    "automation_allowed": True,
    "evaluation": {
        "profit_target": 3000,
        "max_drawdown": {"amount": 2000, "trailing": "end_of_day", "locks_at": 2100},
        "daily_loss_limit": None,
        "consistency": None,
        "min_trading_days": 0,
    },
    "funded": {
        "max_drawdown": {"amount": 2000, "trailing": "end_of_day", "locks_at": 2100},
        "daily_loss_limit": None,
        "consistency": None,
        "payout": {"max_pct_of_profit": 50, "split_pct": 90, "min_days": 0, "min_profit": 0},
    },
    "session": {"flat_by": "16:45", "tz": "America/New_York"},
    "contracts": {"max_minis": 4, "micros_per_mini": 10},
    "prohibited": {"microscalp": {"max_profit_pct": 50, "hold_seconds": 5}, "hft": True},
    "costs": {
        "activation_fee": 0,
        "commission_per_side": {"ES": 0, "MES": 0, "NQ": 0, "MNQ": 0},
    },
}

BLANK = AccountState(
    phase="evaluation",
    day=date(2026, 10, 13),
    balance=0.0,
    open_pnl=0.0,
    position=0,
    peak=0.0,
    floor=-2000.0,
    day_start_balance=0.0,
    closed_days=(),
    traded_today=False,
    halted_today=False,
    possible_microscalp_pnl=0.0,
)


@pytest.fixture
def base() -> dict:
    """A fresh, valid account-rules dict; tests mutate it freely."""
    return copy.deepcopy(BASE)


@pytest.fixture
def make_state():
    """make_state(balance=..., ...) -> a flat evaluation state on 2026-10-13 with a -2000 floor."""
    return lambda **changes: replace(BLANK, **changes)


@pytest.fixture
def et():
    """et("16:45", day) -> that New York wall-clock time as a UTC datetime."""

    def at(hhmm: str, day: date = date(2026, 10, 13)) -> datetime:
        h, m = map(int, hhmm.split(":"))
        local = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(UTC)

    return at
