import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from fenrir.cli.main import app as cli
from fenrir.core.bars import Bar
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.pull import PullRunner
from fenrir.server.routes import data as data_routes
from fenrir.server.routes.data import front_months
from fenrir.settings import Settings

KEY = "k" * 43
BASE = "http://localhost:8000"
COOKIE = {"cookie": f"{SESSION_COOKIE}={KEY}"}
WRITE = COOKIE | {"origin": BASE}
DAY = date(2026, 10, 6)  # a Tuesday, inside the calendar and outside a roll window


@pytest.fixture(autouse=True)
def _repo_cwd(monkeypatch):
    # load_instruments reads instruments.yaml from the cwd, as the server does
    monkeypatch.chdir(Path(__file__).parents[2])


def _client(data_dir: Path, **settings) -> tuple:
    app = create_app(Settings(data_dir=data_dir, **settings), KEY, "c" * 22, None, port=8000)
    return app, TestClient(app, base_url=BASE)


def _bar(day: date, contract: str) -> Bar:
    return Bar(datetime(day.year, day.month, day.day, 15, tzinfo=UTC), 1, 1, 1, 1, 1, contract)


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_front_months_outside_calendar_is_none():
    assert front_months(load_instruments(), date(1900, 1, 2)) == {"ES": None, "NQ": None}


def test_front_months_inside_calendar():
    instruments = load_instruments()
    expected = {s: front_month(instruments, s, DAY) for s in ("ES", "NQ")}
    assert front_months(instruments, DAY) == expected == {"ES": "ESZ26", "NQ": "NQZ26"}


def test_get_on_empty_data_dir_is_zeros(tmp_path, monkeypatch):
    monkeypatch.setattr(data_routes, "trading_day", lambda ts: DAY)
    r = _client(tmp_path)[1].get("/api/data", headers=COOKIE)
    assert r.status_code == 200
    body = r.json()
    assert body["series"] == [
        {"series": s, "first_day": None, "last_day": None, "days": 0, "gaps": []}
        for s in ("ES", "NQ")
    ]
    rolls = load_instruments().rolls
    assert len(body["rolls"]) == len(rolls)
    assert body["rolls"][0] == {
        "series": rolls[0].series,
        "start": rolls[0].start.isoformat(),
        "contract": rolls[0].contract,
    }
    assert body["front"] == {"ES": "ESZ26", "NQ": "NQZ26"}
    assert body["pull"] == {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "returncode": None,
        "log_tail": [],
    }


def test_front_is_null_outside_the_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(data_routes, "trading_day", lambda ts: date(1900, 1, 2))
    body = _client(tmp_path)[1].get("/api/data", headers=COOKIE).json()
    assert body["front"] == {"ES": None, "NQ": None}


def test_series_match_the_cli_summary(tmp_path, monkeypatch):
    write_bars(tmp_path, "NQ", "NQZ26", [_bar(DAY, "NQZ26"), _bar(date(2026, 10, 8), "NQZ26")])
    monkeypatch.setenv("FENRIR_DATA_DIR", str(tmp_path))
    summary = json.loads(CliRunner().invoke(cli, ["data", "summary", "--json"]).output)
    series = _client(tmp_path)[1].get("/api/data", headers=COOKIE).json()["series"]
    assert series == summary["series"]
    assert series[1] == {
        "series": "NQ",
        "first_day": "2026-10-06",
        "last_day": "2026-10-08",
        "days": 2,
        "gaps": [["2026-10-07", "2026-10-07"]],
    }


def test_create_app_holds_an_idle_runner(tmp_path):
    app, _ = _client(tmp_path)
    assert isinstance(app.state.pull, PullRunner)
    assert app.state.pull.status()["state"] == "idle"


def test_create_app_runs_the_cli_pull_with_the_lse_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "fenrir.server.app.PullRunner",
        lambda cmd, env, *, settings: calls.append((cmd, env, settings)),
    )
    app, _ = _client(tmp_path, lse_api_key="lse-secret")
    [(cmd, env, settings)] = calls
    assert cmd[-2:] == ["data", "pull"]
    assert env["FENRIR_LSE_API_KEY"] == "lse-secret"
    assert settings is app.state.settings  # checked for isolation on every start
    assert app.state.pull is None  # the recorder's return: create_app keeps what it built


def test_unisolated_image_pull_is_500(tmp_path, monkeypatch):
    app, client = _client(tmp_path)
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    response = client.post("/api/data/pull", headers=WRITE)
    assert response.status_code == 500
    assert response.json()["detail"].startswith("refusing to pull: ")
    assert client.get("/api/data", headers=COOKIE).json()["pull"]["state"] == "idle"


def test_pull_202_then_409_while_running(tmp_path):
    app, client = _client(tmp_path)
    app.state.pull = PullRunner([sys.executable, "-c", "import time; time.sleep(1)"])
    first = client.post("/api/data/pull", headers=WRITE)
    assert (first.status_code, first.content) == (202, b"")
    assert client.post("/api/data/pull", headers=WRITE).status_code == 409
    assert client.get("/api/data", headers=COOKIE).json()["pull"]["state"] == "running"
    wait_for(lambda: client.get("/api/data", headers=COOKIE).json()["pull"]["state"] == "done")


def test_401_without_cookie(tmp_path):
    client = _client(tmp_path)[1]
    assert client.get("/api/data").status_code == 401
    assert client.post("/api/data/pull", headers={"origin": BASE}).status_code == 401


def test_cross_origin_pull_is_403(tmp_path):
    app, client = _client(tmp_path)
    app.state.pull = PullRunner([sys.executable, "-c", "pass"])
    evil = COOKIE | {"origin": "http://evil.example"}
    assert client.post("/api/data/pull", headers=evil).status_code == 403
    assert client.post("/api/data/pull", headers=COOKIE).status_code == 403
    assert app.state.pull.status()["state"] == "idle"
