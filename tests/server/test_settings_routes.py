import os
import stat
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from fenrir.live import journal
from fenrir.live.arming import Armed, insert_armed
from fenrir.rules.loader import list_accounts
from fenrir.server import isolation
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.jobs import Job, JobItem
from fenrir.server.routes.settings import restart_refusals
from fenrir.settings import Settings, parse_env_text

KEY = "k" * 43
BASE = "http://localhost:8000"
WRITE = {"origin": BASE}
RULES = Path(__file__).parents[2] / "rules"
AID = "lucid-flex-50k"
LSE = "lse-secret-key-0001"
TOKEN = "nt-secret-token-0002"
ENV = (
    "# my settings\r\n"
    f"FENRIR_LSE_API_KEY={LSE}\r\n"
    "SOMETHING_ELSE=kept  # a note\r\n"
    f'FENRIR_NT_TOKEN="{TOKEN}"\r\n'
    "FENRIR_HOLDOUT_MONTHS=6\r\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch) -> tuple[TestClient, Path, Path]:
    env_file = tmp_path / "private" / ".env"
    env_file.parent.mkdir()
    env_file.write_bytes(ENV.encode())
    monkeypatch.setenv("FENRIR_ENV_FILE", str(env_file))
    monkeypatch.delenv("FENRIR_BIND_ALL", raising=False)
    data_dir = tmp_path / "data"
    settings = Settings(rules_dir=RULES, data_dir=data_dir, private_dir=env_file.parent)
    app = create_app(settings, KEY, "c" * 22, None, port=8000)
    client = TestClient(app, base_url=BASE)
    client.cookies.set(SESSION_COOKIE, KEY)
    return client, env_file, data_dir


def test_needs_a_session(env):
    client, _, _ = env
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings/accounts").status_code == 401


def test_get_masks_secrets_and_reads_the_file(env):
    client, _, _ = env
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {
        "lse_api_key": "••••0001",
        "nt_url": Settings().nt_url,
        "nt_token": "••••0002",
        "holdout_months": 6,
        "safety_margin_pct": 80.0,
        "stale_seconds": 180,
        "nested_git": [],
        "can_restart": False,
        "restart_pending": False,
    }
    assert LSE not in r.text and TOKEN not in r.text


def test_masks_sent_back_leave_secrets_unchanged(env):
    client, env_file, _ = env
    page = client.get("/api/settings").json()
    for k in ("nested_git", "can_restart", "restart_pending"):  # reported, not settings
        del page[k]
    r = client.put("/api/settings", json=page | {"stale_seconds": 30}, headers=WRITE)
    assert r.status_code == 200 and r.json()["stale_seconds"] == 30
    assert LSE not in r.text and TOKEN not in r.text
    saved = parse_env_text(env_file.read_text())
    assert saved["FENRIR_LSE_API_KEY"] == LSE and saved["FENRIR_NT_TOKEN"] == TOKEN
    assert saved["FENRIR_STALE_SECONDS"] == "30"


def test_put_edits_in_place_keeping_comments_and_unknown_lines(env):
    client, env_file, _ = env
    r = client.put("/api/settings", json={"nt_token": "a-new-token-9999"}, headers=WRITE)
    assert r.json()["nt_token"] == "••••9999"
    assert env_file.read_bytes() == ENV.replace(f'"{TOKEN}"', "a-new-token-9999").encode()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_a_secret_can_be_cleared(env):
    client, env_file, _ = env
    assert client.put("/api/settings", json={"lse_api_key": ""}, headers=WRITE).status_code == 200
    assert parse_env_text(env_file.read_text())["FENRIR_LSE_API_KEY"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {"holdout_months": "twelve"},
        {"holdout_months": -1},
        {"holdout_months": 6.5},
        {"safety_margin_pct": 150},
        {"safety_margin_pct": 0},
        {"stale_seconds": True},
        {"nt_url": "ftp://x"},
        {"nt_url": "http://ok:8123\nFENRIR_DATA_DIR=/etc"},
        {"nt_token": "line\nFENRIR_DATA_DIR=/etc"},
        {"nt_token": "a b"},
        {"nt_token": None},
        {"nt_token": "••••9999"},  # a stale mask is not a token
        {"data_dir": "/etc"},
        {"FENRIR_NT_TOKEN": "x"},
        {"stale_seconds": 30, "bogus": 1},
        {"stale_seconds": 30, "holdout_months": "x"},
    ],
)
def test_invalid_values_are_422_and_leave_the_file_byte_identical(env, body):
    client, env_file, _ = env
    r = client.put("/api/settings", json=body, headers=WRITE)
    assert r.status_code == 422
    assert r.json()["errors"] and all({"loc", "msg"} == set(e) for e in r.json()["errors"])
    assert env_file.read_bytes() == ENV.encode()


@pytest.mark.parametrize("raw", [b"[1, 2]", b"not json", b"null"])
def test_non_object_body_is_422(env, raw):
    client, env_file, _ = env
    r = client.put("/api/settings", content=raw, headers=WRITE)
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == []
    assert env_file.read_bytes() == ENV.encode()


def test_a_missing_env_file_is_created(env):
    client, env_file, _ = env
    env_file.unlink()
    assert client.get("/api/settings").json()["lse_api_key"] == ""
    r = client.put(
        "/api/settings",
        json={"lse_api_key": "fresh-key-12345678", "holdout_months": 3},
        headers=WRITE,
    )
    assert r.status_code == 200
    assert parse_env_text(env_file.read_text()) == {
        "FENRIR_LSE_API_KEY": "fresh-key-12345678",
        "FENRIR_HOLDOUT_MONTHS": "3",
    }
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_a_symlinked_env_file_is_replaced_not_written_through(env, tmp_path):
    client, env_file, _ = env
    target = tmp_path / "elsewhere"
    target.write_text("untouched\n")
    env_file.unlink()
    env_file.symlink_to(target)
    assert client.put("/api/settings", json={"stale_seconds": 30}, headers=WRITE).status_code == 200
    assert target.read_text() == "untouched\n"
    assert not env_file.is_symlink()


def test_settings_report_a_nested_git_in_the_image(env, tmp_path, monkeypatch):
    client, _, _ = env
    (tmp_path / "app" / "strategies").mkdir(parents=True)
    (tmp_path / "app" / "strategies" / ".git").write_text("gitdir: ../data/x\n")
    monkeypatch.setattr(isolation, "APP_ROOT", tmp_path / "app")
    assert client.get("/api/settings").json()["nested_git"] == []  # natively: not checked
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    assert client.get("/api/settings").json()["nested_git"] == ["strategies/.git"]
    r = client.put("/api/settings", json={"stale_seconds": 30}, headers=WRITE)
    assert r.json()["nested_git"] == ["strategies/.git"]
    assert client.put("/api/settings", json={"nested_git": []}, headers=WRITE).status_code == 422


def test_accounts_default_for_every_rules_account(env):
    client, _, _ = env
    r = client.get("/api/settings/accounts")
    assert r.status_code == 200
    assert set(r.json()) == set(list_accounts(RULES))
    assert r.json()[AID] == {"eval_price": 0.0, "dll_on": False}


def test_accounts_with_a_symlinked_rules_dir_is_500_listing_nothing(tmp_path, env):
    client, _, _ = env
    linked = tmp_path / "linked"
    linked.symlink_to(RULES, target_is_directory=True)
    client.app.state.settings = Settings(rules_dir=linked, data_dir=tmp_path / "data")
    detail = {"detail": f"{linked} is a symlink; not reading it"}
    for r in (
        client.get("/api/settings/accounts"),
        client.put("/api/settings/accounts", json={}, headers=WRITE),
    ):
        assert (r.status_code, r.json()) == (500, detail)


def test_accounts_put_merges(env):
    client, _, data_dir = env
    data_dir.mkdir()
    path = data_dir / "accounts.yaml"
    path.write_text(
        f"{AID}: {{eval_price: 99.0, dll_on: false}}\nretired-1k: {{eval_price: 5.0}}\n"
    )
    r = client.put(
        "/api/settings/accounts",
        json={AID: {"dll_on": True}, "lucid-pro-50k": {"eval_price": 120}},
        headers=WRITE,
    )
    assert r.status_code == 200
    assert r.json()[AID] == {"eval_price": 99.0, "dll_on": True}
    assert r.json()["lucid-pro-50k"] == {"eval_price": 120.0, "dll_on": False}
    assert yaml.safe_load(path.read_text()) == {
        AID: {"eval_price": 99.0, "dll_on": True},
        "lucid-pro-50k": {"eval_price": 120.0, "dll_on": False},
        "retired-1k": {"eval_price": 5.0, "dll_on": False},
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert client.get("/api/settings/accounts").json() == r.json()


def test_accounts_put_creates_the_file(env):
    client, _, data_dir = env
    r = client.put("/api/settings/accounts", json={AID: {"eval_price": 50}}, headers=WRITE)
    assert r.status_code == 200
    assert yaml.safe_load((data_dir / "accounts.yaml").read_text()) == {
        AID: {"eval_price": 50.0, "dll_on": False}
    }


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"lucid-nope-1k": {"dll_on": True}}, ["lucid-nope-1k"]),
        ({AID: {"color": "red"}}, [AID, "color"]),
        ({AID: {"eval_price": -1}}, [AID, "eval_price"]),
        ({AID: {"eval_price": "50"}}, [AID, "eval_price"]),
        ({AID: {"dll_on": "true"}}, [AID, "dll_on"]),
        ({AID: True}, [AID]),
        ({"lucid-pro-50k": {"eval_price": 10}, AID: {"dll_on": 1}}, [AID, "dll_on"]),
    ],
)
def test_accounts_put_rejects_bad_input_and_writes_nothing(env, body, loc):
    client, _, data_dir = env
    data_dir.mkdir()
    path = data_dir / "accounts.yaml"
    path.write_text("# mine\n")
    r = client.put("/api/settings/accounts", json=body, headers=WRITE)
    assert r.status_code == 422
    assert loc in [e["loc"] for e in r.json()["errors"]]
    assert path.read_text() == "# mine\n"


def test_a_broken_accounts_file_is_500_and_never_overwritten(env):
    client, _, data_dir = env
    data_dir.mkdir()
    path = data_dir / "accounts.yaml"
    path.write_text(f"{AID}: {{eval_price: lots}}\n")
    assert "accounts.yaml" in client.get("/api/settings/accounts").json()["detail"]
    r = client.put("/api/settings/accounts", json={AID: {"dll_on": True}}, headers=WRITE)
    assert r.status_code == 500
    assert path.read_text() == f"{AID}: {{eval_price: lots}}\n"


def test_put_without_origin_is_403(env):
    client, env_file, data_dir = env
    assert client.put("/api/settings", json={"stale_seconds": 30}).status_code == 403
    assert client.put("/api/settings/accounts", json={AID: {"dll_on": True}}).status_code == 403
    assert env_file.read_bytes() == ENV.encode() and not data_dir.exists()


def test_a_bad_value_in_the_file_is_500_and_put_writes_nothing(env):
    client, env_file, _ = env
    env_file.write_bytes(ENV.encode() + b"FENRIR_STALE_SECONDS=soon\r\n")
    before = env_file.read_bytes()
    assert client.get("/api/settings").status_code == 500
    assert client.put("/api/settings", json={"holdout_months": 3}, headers=WRITE).status_code == 500
    assert env_file.read_bytes() == before


def test_accounts_symlinked_to_the_secrets_file_never_leak(env):
    client, env_file, data_dir = env
    data_dir.mkdir()
    (data_dir / "accounts.yaml").symlink_to(env_file)
    r = client.get("/api/settings/accounts")
    assert r.status_code == 500 and "symlink" in r.json()["detail"]
    r2 = client.put("/api/settings/accounts", json={AID: {"dll_on": True}}, headers=WRITE)
    assert r2.status_code == 500
    for text in (r.text, r2.text):
        assert TOKEN not in text and LSE not in text and "secret" not in text
    assert env_file.read_bytes() == ENV.encode()


def test_a_fifo_accounts_file_does_not_hang(env):
    client, _, data_dir = env
    data_dir.mkdir()
    os.mkfifo(data_dir / "accounts.yaml")
    assert client.get("/api/settings/accounts").status_code == 500


def test_accounts_put_refuses_a_symlinked_data_dir(env, tmp_path):
    client, _, data_dir = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data_dir.symlink_to(elsewhere)
    r = client.put("/api/settings/accounts", json={AID: {"dll_on": True}}, headers=WRITE)
    assert r.status_code == 500 and "symlink" in r.json()["detail"]
    assert list(elsewhere.iterdir()) == []


def test_a_directory_accounts_file_is_500_naming_it(env):
    client, _, data_dir = env
    (data_dir / "accounts.yaml").mkdir(parents=True)
    r = client.get("/api/settings/accounts")
    assert r.status_code == 500 and "accounts.yaml is not a regular file" in r.json()["detail"]


class _Busy:
    def __init__(self, pull="idle", jobs=()):
        self.pull, self._jobs = pull, list(jobs)

    def status(self) -> dict:
        return {"state": self.pull}

    def jobs(self) -> list[Job]:
        return self._jobs


@pytest.fixture
def image(env, monkeypatch) -> tuple[TestClient, list[str]]:
    client = env[0]
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    calls: list[str] = []
    client.app.state.terminate = lambda: calls.append("terminate")
    return client, calls


def _arm(private_dir: Path, account: str) -> None:
    at = datetime(2026, 10, 14, tzinfo=UTC)
    with closing(journal.open_db(private_dir / "fenrir.db")) as conn:
        insert_armed(
            conn, Armed(account, "strategies/orb.py", "h", AID, "MESM6", "dry", "evaluation", at)
        )


def test_restart_refusals_name_every_reason():
    assert restart_refusals(True, False, [], False, False) == []
    assert "natively" in restart_refusals(False, False, ["A"], True, True)[0]
    assert restart_refusals(True, True, ["A"], False, False) == [
        "The server is already restarting."
    ]
    why = restart_refusals(True, False, ["A", "B"], True, True)
    assert why[0] == "Disarm every account first (armed: A, B)."
    assert len(why) == 3 and "data pull" in why[1] and "backtest job" in why[2]


def test_restart_in_the_image_is_202_then_terminates_once(image):
    client, calls = image
    r = client.post("/api/settings/restart", headers=WRITE)
    assert r.status_code == 202 and r.content == b""
    assert calls == ["terminate"]  # a background task: run after the response was sent
    r = client.post("/api/settings/restart", headers=WRITE)
    assert r.status_code == 409 and r.json()["detail"] == "The server is already restarting."
    assert calls == ["terminate"]


def test_restart_natively_is_409_and_never_terminates(env):
    client = env[0]
    calls = []
    client.app.state.terminate = lambda: calls.append(1)
    r = client.post("/api/settings/restart", headers=WRITE)
    assert r.status_code == 409 and "restart the server yourself" in r.json()["detail"].lower()
    assert calls == []


def test_restart_while_armed_is_409(image, env):
    client, calls = image
    _arm(env[1].parent, "Sim101")
    r = client.post("/api/settings/restart", headers=WRITE)
    assert r.status_code == 409
    assert r.json()["detail"] == "Disarm every account first (armed: Sim101)."
    assert calls == [] and client.app.state.restarting is False


def test_restart_during_a_pull_or_a_backtest_is_409(image):
    client, calls = image
    state = client.app.state
    state.pull = _Busy(pull="running")
    assert "data pull" in client.post("/api/settings/restart", headers=WRITE).json()["detail"]
    state.pull = _Busy()
    item = JobItem("strategies/orb.py", AID, "MESM6")
    state.jobs = _Busy(jobs=[Job("j1", "2026-10-14T00:00:00Z", False, (item,))])
    r = client.post("/api/settings/restart", headers=WRITE)
    assert r.status_code == 409 and "backtest job" in r.json()["detail"]
    state.jobs = _Busy(
        jobs=[Job("j1", "2026-10-14T00:00:00Z", False, (replace(item, status="done"),))]
    )
    assert client.post("/api/settings/restart", headers=WRITE).status_code == 202
    assert calls == ["terminate"]


def test_restart_needs_the_session_and_our_origin(image):
    client, calls = image
    assert client.post("/api/settings/restart").status_code == 403  # no Origin
    evil = {"origin": "http://evil.example"}
    assert client.post("/api/settings/restart", headers=evil).status_code == 403
    cross = WRITE | {"sec-fetch-site": "cross-site"}
    assert client.post("/api/settings/restart", headers=cross).status_code == 403
    client.cookies.clear()
    assert client.post("/api/settings/restart", headers=WRITE).status_code == 401
    assert calls == []


def test_settings_say_whether_a_restart_applies_and_works(env, monkeypatch):
    client = env[0]
    page = client.get("/api/settings").json()
    assert page["can_restart"] is False and page["restart_pending"] is False
    r = client.put("/api/settings", json={"stale_seconds": 180}, headers=WRITE)  # unchanged
    assert r.json()["restart_pending"] is False
    r = client.put("/api/settings", json={"stale_seconds": 30}, headers=WRITE)
    assert r.json()["restart_pending"] is True
    assert client.get("/api/settings").json()["restart_pending"] is True
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    assert client.get("/api/settings").json()["can_restart"] is True
    bad = client.put("/api/settings", json={"restart_pending": False}, headers=WRITE)
    assert bad.status_code == 422  # reported, not settings
