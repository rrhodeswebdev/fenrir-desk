"""Pydantic models for one prop-firm account type's rules (spec §4). Pure: no I/O."""

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

Pct = Annotated[float, Field(gt=0, le=100)]
Money = Annotated[float, Field(ge=0)]


class _Model(BaseModel):
    # strict: no "4" -> 4 or true -> 1.0; no inf/nan, which would disable a limit
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


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
    min_profit: Money  # smallest payout request
    min_day_profit: Money = 0  # a day counts toward min_days only at or above this
    max_amount: float | None = Field(default=None, gt=0)  # cap per payout
    max_payouts: int | None = Field(default=None, gt=0)  # then the account moves on (e.g. live)


class Phase(_Model):
    """What both phases share; plan 06 judges either phase through this."""

    max_drawdown: Drawdown
    daily_loss_limit: float | None = Field(default=None, gt=0)
    consistency: Consistency | None = None
    min_trading_days: int = Field(default=0, ge=0)


class Evaluation(Phase):
    profit_target: float = Field(gt=0)


class Tier(_Model):
    from_profit: Money
    max_minis: int = Field(gt=0)


class Funded(Phase):
    payout: Payout
    # max size by profit, recalculated at session end; None = contracts.max_minis throughout
    scaling: tuple[Tier, ...] | None = Field(default=None, strict=False)  # lax: YAML gives a list

    @field_validator("scaling")
    @classmethod
    def _tiers_ascend_from_zero(cls, tiers: tuple[Tier, ...] | None) -> tuple[Tier, ...] | None:
        if tiers is None:
            return None
        starts = [t.from_profit for t in tiers]
        if starts[:1] != [0] or starts != sorted(set(starts)):
            raise ValueError("tiers must start at from_profit 0 and strictly increase")
        return tiers


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


class Commission(_Model):
    ES: Money
    MES: Money
    NQ: Money
    MNQ: Money


class Costs(_Model):
    # no eval_fee: the price paid varies with coupons, so it is an attempts-report input
    activation_fee: Money
    commission_per_side: Commission


class AccountRules(_Model):
    firm: str = Field(pattern=r"^[a-z0-9]+$")
    account: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    # None = not yet verified; lax so JSON/form "2026-10-10" strings parse
    verified_on: date | None = Field(strict=False)
    source_url: str
    automation_allowed: bool
    evaluation: Evaluation | None  # None for accounts with no evaluation (Lucid Direct)
    funded: Funded
    session: Session
    contracts: Contracts
    prohibited: Prohibited
    costs: Costs
