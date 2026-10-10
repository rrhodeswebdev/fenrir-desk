import pytest
from pydantic import ValidationError

from fenrir.rules.schema import AccountRules


def test_base_parses(base):
    rules = AccountRules.model_validate(base)
    assert rules.funded.payout.split_pct == 90
    assert rules.evaluation.max_drawdown.trailing == "end_of_day"
    assert rules.evaluation.profit_target == 3000.0


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


def test_commission_needs_every_symbol(base):
    del base["costs"]["commission_per_side"]["MNQ"]
    with pytest.raises(ValidationError, match="MNQ"):
        AccountRules.model_validate(base)


def test_model_is_frozen(base):
    rules = AccountRules.model_validate(base)
    with pytest.raises(ValidationError):
        rules.firm = "other"
