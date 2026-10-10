import json
import shutil
from datetime import UTC, datetime, time
from pathlib import Path

import polars as pl
from typer.testing import CliRunner

from fenrir.cli.main import app

REPO = Path(__file__).parents[2]


def setup(tmp_path, monkeypatch, key: str = "test-key"):
    shutil.copy(REPO / "instruments.yaml", tmp_path / "instruments.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FENRIR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FENRIR_LSE_API_KEY", key)


def test_summary_without_data(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["data", "summary"])
    assert result.exit_code == 0
    assert "No bars yet. Run: fenrir data pull" in result.output


def test_pull_requires_key(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, key="")
    result = CliRunner().invoke(app, ["data", "pull"])
    assert result.exit_code == 1
    assert "FENRIR_LSE_API_KEY" in result.output


def test_pull_then_summary_json(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)

    def fake(settings, w):
        ts = datetime.combine(w.last, time(15, 0), tzinfo=UTC)
        return pl.DataFrame(
            {"ts": [ts], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]}
        )

    monkeypatch.setattr("fenrir.cli.data.fetch_contract", fake)
    since = datetime.now(UTC).date().replace(day=1).isoformat()
    pulled = CliRunner().invoke(app, ["data", "pull", "--series", "NQ", "--since", since])
    assert pulled.exit_code == 0, pulled.output
    out = json.loads(CliRunner().invoke(app, ["data", "summary", "--json"]).output)
    assert len(out["data_version"]) == 64
    nq = next(s for s in out["series"] if s["series"] == "NQ")
    assert nq["days"] >= 1
