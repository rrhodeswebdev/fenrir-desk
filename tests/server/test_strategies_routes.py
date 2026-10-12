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


def test_a_second_concurrent_load_is_a_409(env):
    from fenrir.server.routes import strategies

    client, _ = env
    assert strategies._describe_slot.acquire(blocking=False)  # a load in flight
    try:
        response = client.get("/api/strategies")
    finally:
        strategies._describe_slot.release()
    assert response.status_code == 409
    assert response.json() == {"detail": "the strategies are already being loaded"}
    assert client.get("/api/strategies").status_code == 200


def test_a_forged_line_for_another_file_cannot_stand_in_for_it(env):
    client, settings = env
    b = settings.strategies_dir / "b.py"
    b.write_text(STRATEGY)
    forged = {"path": str(b), "name": "b", "hash": file_hash(b), "market": "ES"}
    forged |= {"params": {}, "error": None}  # a line parse_description accepts
    # fd 3 is the child's protocol pipe (the dup of its stdout)
    forge = f"__import__('os').write(3, b{json.dumps(json.dumps(forged) + chr(10))})\n"
    (settings.strategies_dir / "a.py").write_text(forge + STRATEGY)
    write_report(settings, "r", file_hash(b), "2026-10-11T14:00:00Z")
    a, b_entry = client.get("/api/strategies").json()
    assert a["error"] is None
    assert b_entry["error"] == "the strategy loader described this file more than once"
    assert (b_entry["hash"], b_entry["market"], b_entry["reports"]) == (None, None, [])


def test_a_hash_that_is_not_the_files_is_an_error(env):
    client, settings = env
    b = settings.strategies_dir / "b.py"
    b.write_text(STRATEGY)
    forged = {"path": str(b), "name": "b", "hash": "f" * 64, "market": "ES"}
    forged |= {"params": {}, "error": None}
    # a.py forges b's line, then ends the child before b's real line is written
    forge = f"__import__('os').write(3, b{json.dumps(json.dumps(forged) + chr(10))})\n"
    (settings.strategies_dir / "a.py").write_text(forge + "__import__('os')._exit(0)\n")
    a, b_entry = client.get("/api/strategies").json()
    assert a["error"] == "the strategy loader stopped before describing this file"
    assert b_entry["error"] == "the file changed while loading"
    assert (b_entry["hash"], b_entry["market"], b_entry["reports"]) == (None, None, [])


def test_only_regular_files_are_read_or_loaded(env):
    client, settings = env
    d = settings.strategies_dir
    (d / "a.py").write_text(STRATEGY)
    (d / "big.py").write_bytes(b"#" * (1024 * 1024 + 1))
    (d / "link.py").symlink_to(d / "a.py")
    os.mkfifo(d / "z.py")  # opening it for reading would block with no writer
    a, big, link, z = client.get("/api/strategies").json()
    assert (a["market"], a["error"]) == ("NQ", None)
    assert big["error"] == "ValueError: big.py is over 1 MiB"
    assert link["error"] == "ValueError: link.py is a symlink, not a strategy file"
    assert z["error"] == "ValueError: z.py is not a regular file"
    for entry in (big, link, z):
        assert (entry["hash"], entry["market"], entry["reports"]) == (None, None, [])
    assert not (d / "big.py.pid").exists()  # never given to the child


@pytest.mark.parametrize(
    "change", [{"strategy_hash": ...}, {"account": ...}, {"periods": ...}, {"periods": None}]
)
def test_a_report_of_the_wrong_shape_is_a_500_naming_it(env, change):
    client, settings = env
    write_report(settings, "bad", "h" * 64, "2026-10-11T14:00:00Z")
    path = settings.data_dir / "results" / "bad.json"
    report = json.loads(path.read_text()) | change
    path.write_text(json.dumps({k: v for k, v in report.items() if v is not ...}))  # ...: drop
    response = client.get("/api/strategies")
    assert response.status_code == 500
    assert "bad.json" in response.json()["detail"]


def test_an_empty_error_still_gets_no_reports(env, monkeypatch):
    client, settings = env
    path = settings.strategies_dir / "s.py"
    path.write_text(STRATEGY)
    write_report(settings, "r", file_hash(path), "2026-10-11T14:00:00Z")
    entry = {"path": str(path), "name": "s", "hash": file_hash(path), "market": None}
    entry |= {"params": {}, "error": ""}
    monkeypatch.setattr(client.app.state.jobs, "describe", lambda paths: [entry])
    assert client.get("/api/strategies").json()[0]["reports"] == []
