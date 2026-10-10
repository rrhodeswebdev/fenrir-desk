import copy

import pytest

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
        "eval_fee": 0,
        "activation_fee": 0,
        "commission_per_side": {"ES": 0, "MES": 0, "NQ": 0, "MNQ": 0},
    },
}


@pytest.fixture
def base() -> dict:
    """A fresh, valid account-rules dict; tests mutate it freely."""
    return copy.deepcopy(BASE)
