from dataclasses import replace
from datetime import UTC, datetime

import pytest
from backtest_helpers import make_rules
from fastapi.testclient import TestClient
from live_helpers import make_setup

from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal
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
    app = create_app(settings, KEY, "c" * 22, None, port=8000)
    c = TestClient(app, base_url=BASE, headers={"origin": BASE})
    c.cookies.set(SESSION_COOKIE, KEY)
    return c


def test_401_without_auth(settings):
    anon = TestClient(create_app(settings, KEY, "c" * 22, None, port=8000), base_url=BASE)
    assert anon.get("/api/live/status").status_code == 401
    assert anon.get("/api/live/journal?account=Sim101&day=2026-10-12").status_code == 401
    assert anon.post("/api/live/kill", headers={"origin": BASE}).status_code == 401
    resolve = {"json": {"account": "Sim101"}, "headers": {"origin": BASE}}
    assert anon.post("/api/live/resolve", **resolve).status_code == 401


def test_kill_and_resolve_refuse_another_origin(client, settings):
    conn = journal.open_db(settings.private_dir / "fenrir.db")
    arming.insert_armed(conn, make_setup(make_rules()).armed)
    foreign = {"origin": "http://evil.example"}
    assert client.post("/api/live/kill", headers=foreign).status_code == 403
    assert arming.list_armed(conn) != ()  # nothing disarmed
    conn.close()
    resolve = client.post("/api/live/resolve", json={"account": "Sim101"}, headers=foreign)
    assert resolve.status_code == 403


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


def test_kill_disarms_all_and_journals_it(client, settings):
    conn = journal.open_db(settings.private_dir / "fenrir.db")
    dry = make_setup(make_rules()).armed
    for account in ("Sim101", "Dry2"):
        arming.insert_armed(conn, replace(dry, account=account))
    r = client.post("/api/live/kill")
    assert (r.status_code, r.json()) == (200, {"ok": True, "error": None})
    assert arming.list_armed(conn) == ()
    for account in ("Sim101", "Dry2"):
        [halt, alert] = journal.rows(conn, account)
        assert (halt.kind, halt.payload) == (
            "halt",
            {"reason": "disarmed", "kind": None, "until": None},
        )
        assert alert.kind == "alert"
        assert alert.payload["message"] == "kill switch: disarmed; flattened and cancelled all"
    conn.close()


def test_kill_reports_a_failed_flatten_and_still_disarms(client, settings):
    conn = journal.open_db(settings.private_dir / "fenrir.db")
    arming.insert_armed(conn, make_setup(make_rules(), mode="sim").armed)
    body = client.post("/api/live/kill").json()  # no add-on configured: nothing flattened
    assert body["ok"] is False and "not configured" in body["error"]
    assert arming.list_armed(conn) == ()
    conn.close()


def test_kill_with_nothing_armed_is_ok_and_creates_no_database(client, settings):
    assert client.post("/api/live/kill").json() == {"ok": True, "error": None}
    assert not (settings.private_dir / "fenrir.db").exists()


def test_resolve_is_404_for_an_account_that_is_not_running(client):
    r = client.post("/api/live/resolve", json={"account": "Sim999"})
    assert r.status_code == 404


def test_resolve_a_running_account(client):
    live = client.app.state.live
    live.states["Sim101"] = core.start(make_setup(make_rules()), T0)[0]
    assert client.post("/api/live/resolve", json={"account": "Sim101"}).json() == {"ok": True}
    # sim with no add-on: NinjaTrader's book cannot be read, so nothing is lifted
    live.states["Sim101"] = core.start(make_setup(make_rules(), mode="sim"), T0)[0]
    assert client.post("/api/live/resolve", json={"account": "Sim101"}).json() == {"ok": False}


def test_resolve_reports_any_other_hub_error_as_a_500_not_a_404(settings, monkeypatch):
    app = create_app(settings, KEY, "c" * 22, None, port=8000)
    c = TestClient(app, base_url=BASE, headers={"origin": BASE}, raise_server_exceptions=False)
    c.cookies.set(SESSION_COOKIE, KEY)
    app.state.live.states["Sim101"] = core.start(make_setup(make_rules()), T0)[0]

    async def outside_the_calendar(*args):
        raise ValueError("no front month for 2031-01-02")

    monkeypatch.setattr(app.state.live, "_reconcile", outside_the_calendar)
    assert c.post("/api/live/resolve", json={"account": "Sim101"}).status_code == 500


def test_resolve_needs_exactly_an_account(client):
    assert client.post("/api/live/resolve", json={}).status_code == 422
    extra = {"account": "Sim101", "force": True}
    assert client.post("/api/live/resolve", json=extra).status_code == 422
