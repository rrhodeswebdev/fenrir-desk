from datetime import UTC, datetime

import pytest
from backtest_helpers import make_rules
from fastapi.testclient import TestClient
from live_helpers import make_setup

from fenrir.core.bars import Bar
from fenrir.live import core, journal
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.settings import Settings

KEY = "k" * 43
BASE = "http://localhost:8000"
T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET, trading day 2026-10-12


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")


@pytest.fixture
def client(settings) -> TestClient:
    c = TestClient(create_app(settings, KEY, "c" * 22, None, port=8000), base_url=BASE)
    c.cookies.set(SESSION_COOKIE, KEY)
    return c


def test_401_without_auth(settings):
    anon = TestClient(create_app(settings, KEY, "c" * 22, None, port=8000), base_url=BASE)
    assert anon.get("/api/live/status").status_code == 401
    assert anon.get("/api/live/journal?account=Sim101&day=2026-10-12").status_code == 401


def test_status_lists_armed_accounts_with_limits_and_session(client):
    s, _ = core.start(make_setup(make_rules()), T0)
    s, _ = core.on_bar(s, Bar(T0, 20000.0, 20005.0, 19995.0, 20000.0, 10, "NQZ26"))
    client.app.state.live.states["Sim101"] = s
    body = client.get("/api/live/status").json()
    assert body["lse"] == {"connected": False, "last_bar_ts": None}
    assert body["nt"] == {"connected": False, "last_heartbeat": None}
    assert body["drift"] == {"ok": True, "ticks": None}
    [acc] = body["accounts"]
    assert (acc["account"], acc["mode"], acc["phase"], acc["halted"]) == (
        "Sim101",
        "dry",
        "evaluation",
        False,
    )
    assert set(acc["limits"]) == {
        "drawdown_remaining",
        "drawdown_max",
        "daily_loss_remaining",
        "daily_loss_max",
        "max_minis",
        "held_minis",
    }
    assert set(acc["session"]) == {"start", "floor", "target", "safety", "equity", "fills"}
    assert acc["session"]["equity"] == [[T0.isoformat(), 0.0]]


def test_journal_filters_by_account_and_trading_day(client, settings):
    conn = journal.open_db(settings.private_dir / "fenrir.db")
    journal.write(conn, T0, "Sim101", "bar", {"n": 1})
    journal.write(conn, T0, "Sim102", "bar", {"n": 2})
    # 18:00 ET: trading day 2026-10-13 starts
    journal.write(conn, datetime(2026, 10, 12, 22, 0, tzinfo=UTC), "Sim101", "bar", {"n": 3})
    conn.close()
    rows = client.get("/api/live/journal?account=Sim101&day=2026-10-12").json()
    assert rows == [
        {"id": 1, "ts": T0.isoformat(), "account": "Sim101", "kind": "bar", "payload": {"n": 1}}
    ]
    assert [
        r["payload"]["n"]
        for r in client.get("/api/live/journal?account=Sim101&day=2026-10-13").json()
    ] == [3]


def test_journal_without_a_database_is_empty_and_creates_none(client, settings):
    assert client.get("/api/live/journal?account=Sim101&day=2026-10-12").json() == []
    assert not (settings.private_dir / "fenrir.db").exists()


def test_journal_needs_an_account_and_a_day(client):
    assert client.get("/api/live/journal?day=2026-10-12").status_code == 422
    assert client.get("/api/live/journal?account=Sim101&day=12/10/2026").status_code == 422
