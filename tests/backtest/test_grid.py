import shutil
from dataclasses import replace
from pathlib import Path

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
    s = replace(Settings(), rules_dir=tmp_path / "rules")
    (tmp_path / "strategies").mkdir()
    for name, market in (("orb_nq", "NQ"), ("orb_es", "ES"), ("fade_nq", "NQ")):
        (tmp_path / "strategies" / f"{name}.py").write_text(STRATEGY.format(market=market))
    (s.rules_dir / "lucid").mkdir(parents=True)
    for account in ("flex-25k", "flex-50k", "pro-50k"):
        (s.rules_dir / "lucid" / f"{account}.yaml").write_text("")
    return s


def grid(settings, strategies, accounts, contracts=None):
    d = settings.rules_dir.parent / "strategies"
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


def test_strategy_globs_are_relative_to_the_cwd_and_ordered(settings, tmp_path, monkeypatch):
    shutil.copy("instruments.yaml", tmp_path)  # read from cwd, never through a symlink
    monkeypatch.chdir(tmp_path)
    items = expand_grid(["strategies/*_nq.py"], ["lucid-flex-*"], None, settings)
    assert items[:4] == tuple(
        GridItem(Path("strategies/fade_nq.py"), a, c)
        for a in ("lucid-flex-25k", "lucid-flex-50k")
        for c in ("NQ", "MNQ")
    )
    assert len(items) == 2 * 2 * 2


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


def test_contract_fitting_no_strategy_raises_even_when_others_fit(settings):
    with pytest.raises(ValueError, match="empty grid: MES fits no strategy"):
        grid(settings, ["orb_nq.py"], ["lucid-*"], ["MNQ", "MES"])


@pytest.mark.parametrize(("strategies", "accounts"), [([], ["lucid-*"]), (["orb_nq.py"], [])])
def test_no_patterns_raises(settings, strategies, accounts):
    with pytest.raises(ValueError, match="no patterns given"):
        grid(settings, strategies, accounts)


def test_empty_contract_list_raises(settings):
    with pytest.raises(ValueError, match="no contracts given"):
        grid(settings, ["orb_nq.py"], ["lucid-*"], [])


def test_unknown_contract_raises(settings):
    with pytest.raises(ValueError, match="XYZ"):
        grid(settings, ["orb_nq.py"], ["lucid-*"], ["XYZ"])
