import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from backtest_helpers import D1, D2, D3, bar, rules_data
from typer.testing import CliRunner

from fenrir.backtest import cache
from fenrir.backtest.grid import GridItem
from fenrir.backtest.run import AccountChoice, run_backtest
from fenrir.cli import backtest as cli_backtest
from fenrir.cli.main import app
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars

PERIOD = {"pass_rate": 0.31, "p_pass_and_payout": 0.19, "expected_gross_per_attempt": 87.5}
REPORT = {
    "strategy": "orb",
    "account": "lucid-flex-50k",
    "contract": "MNQ",
    "periods": {"tuning": PERIOD, "holdout": None},
    "holdout_gap": False,
}


def test_format_summary_tuning_only():
    expected = (
        "orb  lucid-flex-50k  MNQ  tuning: pass 31.0% · pass+payout 19.0% · net $75.00/attempt"
    )
    result = cli_backtest.format_summary(REPORT, {"lucid-flex-50k": AccountChoice(eval_price=12.5)})
    assert result == expected


def test_format_summary_with_holdout_gap():
    holdout = {"pass_rate": 0.2, "p_pass_and_payout": 0.05, "expected_gross_per_attempt": -40.0}
    report = {**REPORT, "periods": {"tuning": PERIOD, "holdout": holdout}, "holdout_gap": True}
    assert cli_backtest.format_summary(report, {}).endswith(
        "hold-out: pass 20.0% · pass+payout 5.0% · net $-40.00/attempt  HOLD-OUT GAP"
    )


ITEMS = (
    GridItem(Path("a.py"), "lucid-flex-50k", "MNQ"),
    GridItem(Path("a.py"), "lucid-pro-50k", "MNQ"),
)


def fake_grid(monkeypatch):
    calls = {"grid": [], "runs": []}

    def grid(strategies, accounts, contracts, settings):
        calls["grid"].append((strategies, accounts, contracts))
        return ITEMS

    def run(item, settings, *, include_holdout, on_progress=None):
        calls["runs"].append((item, include_holdout))
        on_progress(1, 1)
        return REPORT

    monkeypatch.setattr(cli_backtest, "expand_grid", grid)
    monkeypatch.setattr(cli_backtest, "cached_run", run)
    return calls


def test_backtest_expands_globs_tuning_only(monkeypatch):
    calls = fake_grid(monkeypatch)
    result = CliRunner().invoke(app, ["backtest", "strategies/*.py", "--account", "lucid-*"])
    assert result.exit_code == 0, result.output
    assert calls["grid"] == [(["strategies/*.py"], ["lucid-*"], None)]  # no --contract = None
    assert calls["runs"] == [(ITEMS[0], False), (ITEMS[1], False)]
    assert result.output.count("tuning: pass 31.0%") == 2


def test_evaluate_with_contracts_and_json(monkeypatch):
    calls = fake_grid(monkeypatch)
    args = [
        "evaluate",
        "a.py",
        "b*.py",
        "--account",
        "lucid-flex-50k",
        "--account",
        "lucid-pro-*",
        "--contract",
        "MNQ",
        "--contract",
        "NQ",
        "--json",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls["grid"] == [(["a.py", "b*.py"], ["lucid-flex-50k", "lucid-pro-*"], ["MNQ", "NQ"])]
    assert [include for _, include in calls["runs"]] == [True, True]
    assert json.loads(result.stdout) == [REPORT, REPORT]  # progress goes to stderr
    assert "a.py lucid-flex-50k MNQ: 1/1 attempts\n" in result.stderr


@pytest.mark.parametrize(
    "error",
    [
        ValueError("MES trades ES prices but orb is an NQ strategy"),
        FileNotFoundError("rules/lucid/flex-75k.yaml"),
    ],
)
def test_errors_exit_1_with_message(monkeypatch, error):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(cli_backtest, "expand_grid", boom)
    result = CliRunner().invoke(app, ["backtest", "a.py", "--account", "x"])
    assert result.exit_code == 1
    assert f"error: {error}" in result.output


def test_missing_strategy_file_error(tmp_path):
    missing = tmp_path / "nonexistent.py"
    result = CliRunner().invoke(app, ["backtest", str(missing), "--account", "x"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert str(missing) in result.output


def test_second_run_is_served_from_cache(tmp_path, monkeypatch):
    data, rules = tmp_path / "data", tmp_path / "rules"
    code = front_month(load_instruments(), "NQ", D1)
    bars = [
        replace(bar(f"09:{m:02d}", p, p + 2, p - 1, p + 1, day), contract=code)
        for day in (D1, D2, D3)
        for m, p in ((m, 20000.0 + 3 * m) for m in range(30, 60))
    ]
    write_bars(data, "NQ", code, bars)
    for size in ("50k", "100k"):
        (rules / "lucid").mkdir(parents=True, exist_ok=True)
        rules_yaml = yaml.safe_dump({**rules_data(), "account": f"flex-{size}"})
        (rules / "lucid" / f"flex-{size}.yaml").write_text(rules_yaml)
    (tmp_path / "strategies").mkdir()
    repo = Path(__file__).parents[2]
    shutil.copy(repo / "strategies" / "orb.py", tmp_path / "strategies")
    shutil.copy(repo / "instruments.yaml", tmp_path)
    monkeypatch.chdir(tmp_path)  # strategy globs and instruments.yaml are cwd-relative
    computed = []

    def counted(*a, **k):
        computed.append(a[1])
        return run_backtest(*a, **k)

    monkeypatch.setattr(cache, "run_backtest", counted)
    env = {"FENRIR_DATA_DIR": str(data), "FENRIR_RULES_DIR": str(rules)}
    env["FENRIR_HOLDOUT_MONTHS"] = "0"
    args = ["backtest", "strategies/*.py", "--account", "lucid-*", "--contract", "MNQ", "--json"]
    first = CliRunner().invoke(app, args, env=env)
    second = CliRunner().invoke(app, args, env=env)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert sorted(computed) == ["lucid-flex-100k", "lucid-flex-50k"]  # once each, not again
    assert second.stdout == first.stdout
    assert [r["account"] for r in json.loads(first.stdout)] == ["lucid-flex-100k", "lucid-flex-50k"]
    assert "attempts" in first.stderr  # run 1 computed and showed progress
    assert second.stderr == ""  # a cache hit reports no progress
