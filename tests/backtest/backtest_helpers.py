"""Builders for synthetic backtest inputs. Times are America/New_York wall-clock."""

from datetime import UTC, date, datetime
from pathlib import Path

from fenrir.core.actions import Action
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import NY
from fenrir.rules.schema import AccountRules
from fenrir.strategy import Strategy

NQ = ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0)
D1, D2, D3 = date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8)  # Mon, Tue, Wed


def et(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(UTC)


def bar(hhmm: str, o: float, hi: float, lo: float, c: float, day: date = D1) -> Bar:
    return Bar(et(day, hhmm), o, hi, lo, c, 100, "NQZ25")


def rules_data(
    *,
    target: float = 3000.0,
    drawdown: float = 2000.0,
    daily_loss: float | None = None,
    commission: float = 0.0,
    evaluation: bool = True,
    activation_fee: float = 0.0,
) -> dict:
    """A Lucid-shaped rules dict (spec §4 + plan 05 `locks_at`)."""
    phase = {
        "max_drawdown": {"amount": drawdown, "trailing": "end_of_day", "locks_at": None},
        "daily_loss_limit": daily_loss,
        "consistency": None,
        "min_trading_days": 0,
    }
    return {
        "firm": "lucid",
        "account": "test-50k",
        "verified_on": "2026-10-10",
        "source_url": "https://example.com/rules",
        "automation_allowed": True,
        "evaluation": {**phase, "profit_target": target} if evaluation else None,
        "funded": {
            **phase,
            "payout": {"max_pct_of_profit": 50, "split_pct": 90, "min_days": 0, "min_profit": 0},
        },
        "session": {"flat_by": "16:45", "tz": "America/New_York"},
        "contracts": {"max_minis": 4, "micros_per_mini": 10},
        "prohibited": {"microscalp": {"max_profit_pct": 50, "hold_seconds": 5}, "hft": True},
        "costs": {
            "activation_fee": activation_fee,
            "commission_per_side": {s: commission for s in ("ES", "MES", "NQ", "MNQ")},
        },
    }


def make_rules(**kw) -> AccountRules:
    return AccountRules.model_validate(rules_data(**kw))


def scripted(script: dict[str, list[Action]]) -> Strategy:
    """A strategy that returns `script["HH:MM"]` (ET) on every bar with that time, every day."""

    def step(state, b, ctx):
        return state, list(script.get(b.ts.astimezone(NY).strftime("%H:%M"), []))

    return Strategy("scripted", Path("scripted.py"), "0" * 64, "NQ", {}, lambda p: None, step)
