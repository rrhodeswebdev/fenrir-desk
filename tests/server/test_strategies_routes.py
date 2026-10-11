import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash

KEY = "k" * 43
BASE = "http://localhost:8000"
# module-level side effect: records which process executed the file
STRATEGY = (
    "open(__file__ + '.pid', 'w').write(str(__import__('os').getpid()))\n"
    'MARKET = "NQ"\nPARAMS = {"range_min": 15, "sides": ("long",)}\n\n'
    "def init(p):\n    return None\n\ndef step(s, b, c):\n    return s, []\n"
)
PERIOD = {
    "pass_rate": 0.3,
    "p_pass_and_payout": 0.2,
    "expected_gross_per_attempt": 50.0,
    "sample_equity": [{"start": "2023-05-01", "outcome": "payout", "points": []}],
}


@pytest.fixture
def env(tmp_path) -> tuple[TestClient, Settings]:
    settings = Settings(data_dir=tmp_path / "data", strategies_dir=tmp_path / "strategies")
    settings.strategies_dir.mkdir()
    (settings.data_dir / "results").mkdir(parents=True)
    (settings.data_dir / "accounts.yaml").write_text("lucid-flex-50k: {eval_price: 80}\n")
    client = TestClient(create_app(settings, KEY, "c" * 22, None, port=8000), base_url=BASE)
    client.cookies.set(SESSION_COOKIE, KEY)
    return client, settings


def write_report(settings: Settings, key: str, strategy_hash: str, created_at: str) -> None:
    report = {
        "key": key,
        "created_at": created_at,
        "strategy": "orb",
        "strategy_hash": strategy_hash,
        "account": "lucid-flex-50k",
        "contract": "MNQ",
        "periods": {"tuning": PERIOD, "holdout": None},
    }
    (settings.data_dir / "results" / f"{key}.json").write_text(json.dumps(report))


def test_lists_strategies_with_reports_of_the_current_hash_only(env):
    client, settings = env
    path = settings.strategies_dir / "orb.py"
    path.write_text(STRATEGY)
    write_report(settings, "old", file_hash(path), "2026-10-10T14:00:00Z")
    write_report(settings, "new", file_hash(path), "2026-10-11T14:00:00Z")
    write_report(settings, "stale", "0" * 64, "2026-10-11T15:00:00Z")
    [entry] = client.get("/api/strategies").json()
    reports = entry.pop("reports")
    assert entry == {
        "path": str(path),
        "name": "orb",
        "hash": file_hash(path),
        "market": "NQ",
        "params": {"range_min": 15, "sides": ["long"]},
        "error": None,
    }
    assert [r["key"] for r in reports] == ["new", "old"]  # newest first
    assert reports[0]["periods"]["tuning"]["expected_net_per_attempt"] == -30.0
    assert "sample_equity" not in reports[0]["periods"]["tuning"]
    assert reports[0]["periods"]["holdout"] is None
    # loaded in the child only, never in the server (DEC-1)
    assert int(Path(f"{path}.pid").read_text()) != os.getpid()


def test_a_broken_file_is_listed_with_its_error(env):
    client, settings = env
    good = settings.strategies_dir / "a.py"
    good.write_text(STRATEGY)
    bad = settings.strategies_dir / "wip.py"
    bad.write_text("def step(:\n")
    write_report(settings, "r", file_hash(bad), "2026-10-11T14:00:00Z")
    first, second = client.get("/api/strategies").json()
    assert (first["name"], first["error"]) == ("a", None)
    assert second["error"].startswith("ValueError: ") and "syntax error" in second["error"]
    assert second | {"error": None} == {
        "path": str(bad),
        "name": "wip",
        "hash": file_hash(bad),
        "market": None,
        "params": {},
        "error": None,
        "reports": [],  # even with a report of its hash
    }


def test_unserializable_params_are_an_error(env):
    client, settings = env
    (settings.strategies_dir / "s.py").write_text(STRATEGY.replace('("long",)', "{1}"))
    [entry] = client.get("/api/strategies").json()
    assert entry["error"].startswith("TypeError: ")
    assert (entry["market"], entry["params"]) == (None, {})


def test_no_strategies_is_an_empty_list(env):
    client, _ = env
    assert client.get("/api/strategies").json() == []


def test_isolation_refusal_errors_every_file_and_loads_none(env, monkeypatch):
    client, settings = env
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    path = settings.strategies_dir / "orb.py"
    path.write_text(STRATEGY)
    [entry] = client.get("/api/strategies").json()
    assert entry["error"].startswith("ValueError: refusing to run strategy code")
    assert "FENRIR_PTY_USER is unset" in entry["error"]
    assert (entry["hash"], entry["market"], entry["reports"]) == (None, None, [])
    assert not Path(f"{path}.pid").exists()


def test_a_corrupt_report_is_a_500_naming_it(env):
    client, settings = env
    (settings.data_dir / "results" / "bad.json").write_text("{")
    response = client.get("/api/strategies")
    assert response.status_code == 500
    assert "bad.json" in response.json()["detail"]


def test_401_without_a_session(env):
    _, settings = env
    client = TestClient(create_app(settings, KEY, "c" * 22, None, port=8000), base_url=BASE)
    assert client.get("/api/strategies").status_code == 401
