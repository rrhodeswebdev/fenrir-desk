from datetime import date

import pytest
from pydantic import ValidationError

from fenrir.rules.schema import AccountRules


def test_base_parses(base):
    rules = AccountRules.model_validate(base)
    assert rules.funded.payout.split_pct == 90
    assert rules.evaluation.max_drawdown.trailing == "end_of_day"
    assert rules.evaluation.profit_target == 3000.0


def test_verified_on_accepts_iso_string(base):
    base["verified_on"] = "2026-10-10"  # what JSON and the rules form send
    assert AccountRules.model_validate(base).verified_on == date(2026, 10, 10)


def test_locks_at_defaults_to_none(base):
    del base["evaluation"]["max_drawdown"]["locks_at"]
    assert AccountRules.model_validate(base).evaluation.max_drawdown.locks_at is None


def test_unknown_top_level_key_rejected(base):
    base["news_blackout"] = True
    with pytest.raises(ValidationError, match="news_blackout"):
        AccountRules.model_validate(base)


def test_unknown_nested_key_rejected(base):
    base["funded"]["payout"]["buffer"] = 100
    with pytest.raises(ValidationError, match="buffer"):
        AccountRules.model_validate(base)


def test_direct_has_no_evaluation(base):
    base["evaluation"] = None
    assert AccountRules.model_validate(base).evaluation is None


def test_evaluation_needs_profit_target(base):
    base["evaluation"]["profit_target"] = None
    with pytest.raises(ValidationError, match="profit_target"):
        AccountRules.model_validate(base)


def test_funded_needs_payout(base):
    del base["funded"]["payout"]
    with pytest.raises(ValidationError, match="payout"):
        AccountRules.model_validate(base)


def test_negative_drawdown_rejected(base):
    base["funded"]["max_drawdown"]["amount"] = -2000
    with pytest.raises(ValidationError, match="amount"):
        AccountRules.model_validate(base)


def test_bad_flat_by_rejected(base):
    base["session"]["flat_by"] = "4:45pm"
    with pytest.raises(ValidationError, match="flat_by"):
        AccountRules.model_validate(base)


def test_unknown_time_zone_rejected(base):
    base["session"]["tz"] = "Mars/Olympus"
    with pytest.raises(ValidationError, match="time zone"):
        AccountRules.model_validate(base)


def test_evaluation_with_payout_rejected(base):
    base["evaluation"]["payout"] = base["funded"]["payout"]
    with pytest.raises(ValidationError, match=r"evaluation\.payout\n  Extra inputs"):
        AccountRules.model_validate(base)


def test_funded_with_profit_target_rejected(base):
    base["funded"]["profit_target"] = 3000
    with pytest.raises(ValidationError, match=r"funded\.profit_target\n  Extra inputs"):
        AccountRules.model_validate(base)


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
def test_commission_needs_every_symbol(base, symbol):
    del base["costs"]["commission_per_side"][symbol]
    with pytest.raises(ValidationError, match=f"commission_per_side.{symbol}\n  Field required"):
        AccountRules.model_validate(base)


def test_extra_commission_symbol_rejected(base):
    base["costs"]["commission_per_side"]["YM"] = 0
    with pytest.raises(ValidationError, match="YM"):
        AccountRules.model_validate(base)


def test_negative_commission_rejected(base):
    base["costs"]["commission_per_side"]["ES"] = -1
    with pytest.raises(ValidationError, match="commission_per_side.ES"):
        AccountRules.model_validate(base)


@pytest.mark.parametrize("pct, ok", [(0, False), (0.5, True), (100, True), (101, False)])
def test_pct_bounds(base, pct, ok):
    base["funded"]["payout"]["split_pct"] = pct
    if ok:
        AccountRules.model_validate(base)
    else:
        with pytest.raises(ValidationError, match="split_pct"):
            AccountRules.model_validate(base)


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("max_drawdown", "amount", float("inf")),
        ("max_drawdown", "locks_at", float("inf")),
        (None, "daily_loss_limit", float("inf")),
        ("payout", "min_profit", float("inf")),
        ("payout", "min_profit", float("nan")),
    ],
)
def test_non_finite_numbers_rejected(base, section, key, value):
    phase = base["funded"]
    (phase[section] if section else phase)[key] = value
    with pytest.raises(ValidationError, match=key):
        AccountRules.model_validate(base)


def test_non_finite_commission_rejected(base):
    base["costs"]["commission_per_side"]["ES"] = float("nan")
    with pytest.raises(ValidationError, match="commission_per_side.ES"):
        AccountRules.model_validate(base)


@pytest.mark.parametrize(
    "path, value",
    [
        (("funded", "daily_loss_limit"), True),  # would load as $1
        (("contracts", "max_minis"), "4"),
        (("contracts", "max_minis"), True),
        (("prohibited", "hft"), "yes"),
        (("automation_allowed",), 0),
    ],
)
def test_wrong_types_not_coerced(base, path, value):
    *parents, key = path
    target = base
    for p in parents:
        target = target[p]
    target[key] = value
    with pytest.raises(ValidationError, match=key):
        AccountRules.model_validate(base)


def test_model_is_frozen(base):
    rules = AccountRules.model_validate(base)
    with pytest.raises(ValidationError):
        rules.firm = "other"
    with pytest.raises(ValidationError):
        rules.costs.commission_per_side.ES = -9
    hash(rules)  # usable as a cache key
