import json

from typer.testing import CliRunner

from fenrir.backtest.run import AccountChoice
from fenrir.cli import backtest as cli_backtest
from fenrir.cli.main import app

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


def fake_runs(monkeypatch):
    calls = []

    def fake(path, account, contract, settings, *, include_holdout, on_progress=None):
        calls.append((str(path), account, contract, include_holdout))
        on_progress(1, 1)
        return REPORT

    monkeypatch.setattr(cli_backtest, "run_backtest", fake)
    return calls


def test_backtest_is_tuning_only(monkeypatch):
    calls = fake_runs(monkeypatch)
    args = [
        "backtest",
        "a.py",
        "b.py",
        "--account",
        "lucid-flex-50k",
        "--account",
        "lucid-pro-50k",
        "--contract",
        "MNQ",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls == [
        ("a.py", "lucid-flex-50k", "MNQ", False),
        ("a.py", "lucid-pro-50k", "MNQ", False),
        ("b.py", "lucid-flex-50k", "MNQ", False),
        ("b.py", "lucid-pro-50k", "MNQ", False),
    ]
    assert result.output.count("tuning: pass 31.0%") == 4


def test_evaluate_includes_holdout_and_json(monkeypatch):
    calls = fake_runs(monkeypatch)
    args = ["evaluate", "a.py", "--account", "lucid-flex-50k", "--contract", "MNQ", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls == [("a.py", "lucid-flex-50k", "MNQ", True)]
    assert json.loads(result.stdout) == [REPORT]  # progress goes to stderr
    assert "a.py lucid-flex-50k: 1/1 attempts\n" in result.stderr


def test_errors_exit_1_with_message(monkeypatch):
    def boom(*a, **k):
        raise ValueError("MES trades ES prices but orb is an NQ strategy")

    monkeypatch.setattr(cli_backtest, "run_backtest", boom)
    result = CliRunner().invoke(app, ["backtest", "a.py", "--account", "x", "--contract", "MES"])
    assert result.exit_code == 1
    assert "error: MES trades ES prices" in result.output


def test_missing_strategy_file_error(tmp_path):
    missing = tmp_path / "nonexistent.py"
    result = CliRunner().invoke(
        app, ["backtest", str(missing), "--account", "x", "--contract", "MNQ"]
    )
    assert result.exit_code == 1
    assert "error:" in result.output
    assert str(missing) in result.output
