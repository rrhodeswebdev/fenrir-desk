import json

import pytest
from typer.testing import CliRunner

from fenrir.cli import compare as cli_compare
from fenrir.cli.main import app


def report(strategy, account, p, created, gross=100.0):
    period = {
        "pass_rate": 0.5,
        "p_pass_and_payout": p,
        "expected_gross_per_attempt": gross,
        "sample_equity": [],
    }
    return {
        "key": f"{strategy}-{account}-{created}",
        "created_at": created,
        "strategy": strategy,
        "account": account,
        "contract": "MNQ",
        "periods": {"tuning": period, "holdout": None},
        "holdout_gap": False,
    }


REPORTS = (  # newest first, as list_reports returns them
    report("orb", "lucid-flex-50k", 0.1, "2026-10-10T03:00:00Z"),
    report("vwap", "lucid-flex-50k", 0.3, "2026-10-10T02:00:00Z"),
    report("orb", "lucid-flex-50k", 0.9, "2026-10-10T01:00:00Z"),  # older run, superseded
)


@pytest.fixture
def stored(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_compare, "list_reports", lambda settings: REPORTS)
    (tmp_path / "accounts.yaml").write_text("lucid-flex-50k: {eval_price: 25.0}\n")
    return {"FENRIR_DATA_DIR": str(tmp_path)}


def test_json_prints_groups_of_ranked_latest_rows(stored):
    result = CliRunner().invoke(app, ["compare", "--view", "account", "--json"], env=stored)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert [g["group"] for g in out] == ["lucid-flex-50k"]
    assert [r["strategy"] for r in out[0]["rows"]] == ["vwap", "orb"]
    assert out[0]["rows"][1]["periods"]["tuning"]["expected_net_per_attempt"] == 75.0
    assert "sample_equity" not in out[0]["rows"][0]["periods"]["tuning"]


def test_text_lists_each_group_then_its_rows(stored):
    result = CliRunner().invoke(app, ["compare", "--view", "strategy"], env=stored)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "orb"
    assert lines[1].startswith("  orb  lucid-flex-50k  MNQ  tuning: pass 50.0% · pass+payout 10.0%")
    assert lines[1].endswith("net $75.00/attempt")
    assert lines[2] == "vwap"


def test_unknown_view_is_rejected(stored):
    result = CliRunner().invoke(app, ["compare", "--view", "contract"], env=stored)
    assert result.exit_code == 2


def test_corrupt_report_exits_1_with_message(monkeypatch, tmp_path):
    def boom(settings):
        raise ValueError("data/results/x.json: corrupt cached report, delete it")

    monkeypatch.setattr(cli_compare, "list_reports", boom)
    env = {"FENRIR_DATA_DIR": str(tmp_path)}
    result = CliRunner().invoke(app, ["compare", "--view", "account"], env=env)
    assert result.exit_code == 1
    assert "error: data/results/x.json: corrupt cached report" in result.output
