from dataclasses import replace

import pytest

from fenrir.backtest.grid import GridItem, expand_grid
from fenrir.settings import Settings

STRATEGY = """MARKET = {market!r}
PARAMS = {{}}
def init(p): return None
def step(s, b, c): return s, []
"""


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = replace(Settings(), rules_dir=tmp_path / "rules", strategies_dir=tmp_path / "strategies")
    s.strategies_dir.mkdir()
    for name, market in (("orb_nq", "NQ"), ("orb_es", "ES"), ("fade_nq", "NQ")):
        (s.strategies_dir / f"{name}.py").write_text(STRATEGY.format(market=market))
    (s.rules_dir / "lucid").mkdir(parents=True)
    for account in ("flex-25k", "flex-50k", "pro-50k"):
        (s.rules_dir / "lucid" / f"{account}.yaml").write_text("")
    return s


def grid(settings, strategies, accounts, contracts=None):
    d = settings.strategies_dir
    items = expand_grid([str(d / s) for s in strategies], accounts, contracts, settings)
    return [(i.strategy_path.relative_to(d).as_posix(), i.account_id, i.contract) for i in items]


def test_contracts_none_means_both_symbols_of_the_market(settings):
    assert grid(settings, ["orb_nq.py"], ["lucid-flex-50k"]) == [
        ("orb_nq.py", "lucid-flex-50k", "NQ"),
        ("orb_nq.py", "lucid-flex-50k", "MNQ"),
    ]
    assert grid(settings, ["orb_es.py"], ["lucid-flex-50k"]) == [
        ("orb_es.py", "lucid-flex-50k", "ES"),
        ("orb_es.py", "lucid-flex-50k", "MES"),
    ]


def test_strategy_glob_expands_sorted(settings):
    got = grid(settings, ["*_nq.py"], ["lucid-flex-50k"], ["MNQ"])
    assert got == [("fade_nq.py", "lucid-flex-50k", "MNQ"), ("orb_nq.py", "lucid-flex-50k", "MNQ")]


def test_account_glob_expands_against_rule_files(settings):
    got = grid(settings, ["orb_nq.py"], ["lucid-flex-*"], ["MNQ"])
    assert got == [("orb_nq.py", "lucid-flex-25k", "MNQ"), ("orb_nq.py", "lucid-flex-50k", "MNQ")]


def test_explicit_contracts_keep_only_those_that_fit_each_strategy(settings):
    got = grid(settings, ["orb_*.py"], ["lucid-pro-50k"], ["MNQ", "MES"])
    assert got == [("orb_es.py", "lucid-pro-50k", "MES"), ("orb_nq.py", "lucid-pro-50k", "MNQ")]


def test_overlapping_patterns_give_each_item_once(settings):
    got = grid(settings, ["orb_nq.py", "*_nq.py"], ["lucid-*", "lucid-flex-50k"], ["MNQ", "MNQ"])
    assert len(got) == len(set(got)) == 2 * 3


def test_returns_grid_items_in_a_stable_order(settings):
    a = expand_grid([str(settings.strategies_dir / "*.py")], ["lucid-*"], None, settings)
    b = expand_grid([str(settings.strategies_dir / "*.py")], ["lucid-*"], None, settings)
    assert a == b and all(isinstance(i, GridItem) for i in a)
    assert len(a) == 3 * 3 * 2


@pytest.mark.parametrize(
    ("strategies", "accounts", "pattern"),
    [(["nope_*.py"], ["lucid-*"], "nope_*.py"), (["orb_nq.py"], ["apex-*"], "apex-*")],
)
def test_pattern_matching_nothing_raises_naming_it(settings, strategies, accounts, pattern):
    with pytest.raises(ValueError, match=pattern.replace("*", r"\*")):
        grid(settings, strategies, accounts)


def test_contract_fitting_no_strategy_raises_empty_grid(settings):
    with pytest.raises(ValueError, match="empty grid"):
        grid(settings, ["orb_nq.py"], ["lucid-*"], ["MES"])


def test_unknown_contract_raises(settings):
    with pytest.raises(ValueError, match="XYZ"):
        grid(settings, ["orb_nq.py"], ["lucid-*"], ["XYZ"])
