import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.routes.rules import staleness
from fenrir.settings import Settings

KEY = "k" * 43
BASE = "http://localhost:8000"
WRITE = {"origin": BASE}
RULES = Path(__file__).parents[2] / "rules"
AID = "lucid-flex-50k"


def test_staleness_boundary():
    today = date(2026, 10, 11)
    assert staleness(today - timedelta(days=30), today) == {
        "verified_on": "2026-09-11",
        "age_days": 30,
        "stale": False,
    }
    assert staleness(today - timedelta(days=31), today)["stale"] is True


def test_future_verification_is_stale():
    today = date(2026, 10, 11)
    assert staleness(today + timedelta(days=1), today) == {
        "verified_on": "2026-10-12",
        "age_days": -1,
        "stale": True,
    }


def test_unverified_is_stale():
    assert staleness(None, date(2026, 10, 11)) == {
        "verified_on": None,
        "age_days": None,
        "stale": True,
    }


@pytest.fixture
def env(tmp_path) -> tuple[TestClient, Path]:
    rules_dir = tmp_path / "rules"
    shutil.copytree(RULES, rules_dir)
    (rules_dir / "lucid" / "broken-1k.yaml").write_text(
        "firm: lucid\naccount: broken-1k\nnope: 1\n"
    )
    app = create_app(Settings(rules_dir=rules_dir), KEY, "c" * 22, None, port=8000)
    client = TestClient(app, base_url=BASE)
    client.cookies.set(SESSION_COOKIE, KEY)
    return client, rules_dir


def flex(client: TestClient) -> dict:
    return client.get(f"/api/rules/{AID}").json()["rules"]


def test_list_has_every_account_and_survives_a_broken_file(env):
    client, _ = env
    rows = {r["id"]: r for r in client.get("/api/rules").json()}
    assert len(rows) == 17
    assert rows[AID]["error"] is None and isinstance(rows[AID]["stale"], bool)
    assert rows[AID]["verified_on"] == "2026-10-10"
    assert "nope" in rows["lucid-broken-1k"]["error"]
    assert rows["lucid-broken-1k"]["stale"] is None
    # paths are relative to rules_dir, never the absolute location on disk
    assert rows["lucid-broken-1k"]["error"].startswith("lucid/broken-1k.yaml: ")


def test_get_returns_rules_as_json(env):
    client, _ = env
    body = client.get(f"/api/rules/{AID}").json()
    assert body["error"] is None
    assert body["rules"]["session"] == {"flat_by": "16:45", "tz": "America/New_York"}
    assert body["rules"]["verified_on"] == "2026-10-10"


def test_get_broken_file_has_no_rules(env):
    client, _ = env
    body = client.get("/api/rules/lucid-broken-1k").json()
    assert body["rules"] is None and "nope" in body["error"]


def test_get_unknown_is_404(env):
    client, _ = env
    assert client.get("/api/rules/lucid-nope-1k").status_code == 404
    assert client.get("/api/rules/..%2Fsecret").status_code == 404


def test_put_saves_one_line_and_returns_the_row(env):
    client, rules_dir = env
    path = rules_dir / "lucid" / "flex-50k.yaml"
    before = path.read_text().splitlines()
    rules = flex(client) | {"verified_on": date.today().isoformat()}
    r = client.put(f"/api/rules/{AID}", json=rules, headers=WRITE)
    assert r.status_code == 200
    assert r.json() == {
        "id": AID,
        "error": None,
        "verified_on": date.today().isoformat(),
        "age_days": 0,
        "stale": False,
    }
    after = path.read_text().splitlines()
    assert [(a, b) for a, b in zip(before, after, strict=True) if a != b] == [
        ("verified_on: 2026-10-10", f"verified_on: {date.today().isoformat()}")
    ]


def test_put_unchanged_leaves_file_byte_identical(env):
    client, rules_dir = env
    path = rules_dir / "lucid" / "flex-50k.yaml"
    before = path.read_bytes()
    assert client.put(f"/api/rules/{AID}", json=flex(client), headers=WRITE).status_code == 200
    assert path.read_bytes() == before


def test_put_invalid_is_422_with_field_loc_and_file_unchanged(env):
    client, rules_dir = env
    path = rules_dir / "lucid" / "flex-50k.yaml"
    before = path.read_bytes()
    rules = flex(client)
    rules["funded"]["max_drawdown"]["amount"] = -5
    r = client.put(f"/api/rules/{AID}", json=rules, headers=WRITE)
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)
    assert body["errors"][0]["loc"] == ["funded", "max_drawdown", "amount"]
    assert "greater than 0" in body["errors"][0]["msg"]
    assert path.read_bytes() == before


def test_put_unknown_key_is_422_at_its_loc(env):
    client, _ = env
    r = client.put(f"/api/rules/{AID}", json=flex(client) | {"bogus": 1}, headers=WRITE)
    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["errors"]] == [["bogus"]]


def test_put_rename_is_rejected_at_account(env):
    client, rules_dir = env
    r = client.put(f"/api/rules/{AID}", json=flex(client) | {"account": "flex-100k"}, headers=WRITE)
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == ["account"]
    assert (rules_dir / "lucid" / "flex-100k.yaml").read_text() == (
        RULES / "lucid" / "flex-100k.yaml"
    ).read_text()


def test_put_unknown_account_is_404_and_creates_nothing(env):
    client, rules_dir = env
    rules = flex(client) | {"account": "nope-1k"}
    assert client.put("/api/rules/lucid-nope-1k", json=rules, headers=WRITE).status_code == 404
    assert not (rules_dir / "lucid" / "nope-1k.yaml").exists()


@pytest.mark.parametrize("body", [b"[1, 2]", b"3", b"not json", b""])
def test_put_non_object_is_422_in_the_contract_shape(env, body):
    client, _ = env
    r = client.put(
        f"/api/rules/{AID}",
        content=body,
        headers=WRITE | {"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert r.json()["errors"] == [{"loc": [], "msg": "body must be a JSON object"}]


def test_put_over_a_broken_file_is_422(env):
    client, rules_dir = env
    path = rules_dir / "lucid" / "broken-1k.yaml"
    before = path.read_bytes()
    rules = flex(client) | {"account": "broken-1k"}
    r = client.put("/api/rules/lucid-broken-1k", json=rules, headers=WRITE)
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]
    assert r.json()["detail"].startswith("lucid/broken-1k.yaml: ")
    assert path.read_bytes() == before


def test_rules_api_needs_the_session_cookie(env):
    client, _ = env
    client.cookies.clear()
    assert client.get("/api/rules").status_code == 401
    assert client.get(f"/api/rules/{AID}").status_code == 401


def test_cross_origin_put_is_403(env):
    client, rules_dir = env
    path = rules_dir / "lucid" / "flex-50k.yaml"
    before = path.read_bytes()
    rules = flex(client) | {"verified_on": "2027-01-02"}
    assert client.put(f"/api/rules/{AID}", json=rules).status_code == 403
    evil = {"origin": "http://evil.example"}
    assert client.put(f"/api/rules/{AID}", json=rules, headers=evil).status_code == 403
    assert path.read_bytes() == before
