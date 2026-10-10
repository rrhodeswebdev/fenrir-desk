"""Pydantic models for one prop-firm account type's rules (spec §4). Pure: no I/O."""

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SYMBOLS = frozenset({"ES", "MES", "NQ", "MNQ"})

Pct = Annotated[float, Field(gt=0, le=100)]
Money = Annotated[float, Field(ge=0)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Drawdown(_Model):
    amount: float = Field(gt=0)
    trailing: Literal["end_of_day", "intraday"]
    locks_at: float | None = Field(
        default=None, gt=0
    )  # profit above start where the floor stops trailing


class Consistency(_Model):
    max_day_pct: Pct


class Payout(_Model):
    max_pct_of_profit: Pct
    split_pct: Pct
    min_days: int = Field(ge=0)
    min_profit: Money


class Phase(_Model):
    max_drawdown: Drawdown
    daily_loss_limit: float | None = Field(default=None, gt=0)
    consistency: Consistency | None = None
    min_trading_days: int = Field(default=0, ge=0)
    profit_target: float | None = Field(default=None, gt=0)  # evaluation only
    payout: Payout | None = None  # funded only


class Session(_Model):
    flat_by: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    tz: str

    @field_validator("tz")
    @classmethod
    def _known_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}") from None
        return value


class Contracts(_Model):
    max_minis: int = Field(gt=0)
    micros_per_mini: int = Field(gt=0)


class Microscalp(_Model):
    max_profit_pct: Pct
    hold_seconds: int = Field(gt=0)


class Prohibited(_Model):
    microscalp: Microscalp | None = None
    hft: bool


class Costs(_Model):
    eval_fee: Money
    activation_fee: Money
    commission_per_side: dict[str, float]

    @field_validator("commission_per_side")
    @classmethod
    def _every_symbol(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != SYMBOLS:
            raise ValueError(
                f"commission_per_side needs exactly {sorted(SYMBOLS)}, got {sorted(value)}"
            )
        if any(c < 0 for c in value.values()):
            raise ValueError("commission_per_side values must be >= 0")
        return value


class AccountRules(_Model):
    firm: str = Field(pattern=r"^[a-z0-9]+$")
    account: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    verified_on: date | None  # None = not yet verified against the firm's site
    source_url: str
    automation_allowed: bool
    evaluation: Phase | None  # None for accounts with no evaluation (Lucid Direct)
    funded: Phase
    session: Session
    contracts: Contracts
    prohibited: Prohibited
    costs: Costs

    @model_validator(mode="after")
    def _phase_shapes(self) -> "AccountRules":
        e, f = self.evaluation, self.funded
        if e is not None and (e.profit_target is None or e.payout is not None):
            raise ValueError("evaluation needs profit_target and no payout")
        if f.payout is None or f.profit_target is not None:
            raise ValueError("funded needs payout and no profit_target")
        return self
