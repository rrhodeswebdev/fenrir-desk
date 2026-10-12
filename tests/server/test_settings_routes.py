import os
import stat
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from fenrir.rules.loader import list_accounts
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.routes import settings as routes
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
    app = create_app(Settings(rules_dir=RULES, data_dir=data_dir), KEY, "c" * 22, None, port=8000)
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
        "stale_seconds": 90,
        "repo_env_secrets": False,
    }
    assert LSE not in r.text and TOKEN not in r.text


def test_masks_sent_back_leave_secrets_unchanged(env):
    client, env_file, _ = env
    page = client.get("/api/settings").json()
    del page["repo_env_secrets"]
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


def test_repo_env_secrets_only_in_the_image(env, tmp_path, monkeypatch):
    client, _, _ = env
    repo_env = tmp_path / "repo.env"
    monkeypatch.setattr(routes, "REPO_ENV", repo_env)
    repo_env.write_text("FENRIR_NT_TOKEN=leftover\n")

    def flag() -> bool:
        return client.get("/api/settings").json()["repo_env_secrets"]

    assert flag() is False  # natively the repo .env is the secrets file
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    assert flag() is True
    repo_env.write_text("FENRIR_NT_TOKEN=\nFENRIR_NT_URL=http://x\n")
    assert flag() is False
    repo_env.unlink()
    assert flag() is False
    secrets = tmp_path / "planted"
    secrets.write_text("FENRIR_NT_TOKEN=x\n")
    repo_env.symlink_to(secrets)  # never followed
    assert flag() is False


IMPORT = "/api/settings/import-repo-secrets"


@pytest.fixture
def repo_env(env, tmp_path, monkeypatch) -> Path:
    path = tmp_path / "repo.env"
    monkeypatch.setattr(routes, "REPO_ENV", path)
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    return path


def test_import_fills_only_empty_secrets_and_never_returns_them(env, repo_env):
    client, env_file, _ = env
    env_file.write_bytes(ENV.replace(f'"{TOKEN}"', "").encode())  # token empty, LSE key set
    new_token = "repo-token-secret-9999"
    repo_env.write_text(
        f"FENRIR_NT_TOKEN={new_token}\nFENRIR_LSE_API_KEY=repo-lse-key-8888\nFENRIR_STALE_SECONDS=5\n"
    )
    r = client.post(IMPORT, headers=WRITE)
    assert r.status_code == 200 and new_token not in r.text and "repo-lse" not in r.text
    assert r.json()["imported"] == ["nt_token"]
    assert r.json()["nt_token"] == "••••9999" and r.json()["stale_seconds"] == 90
    saved = parse_env_text(env_file.read_text())
    assert saved["FENRIR_NT_TOKEN"] == new_token and saved["FENRIR_LSE_API_KEY"] == LSE
    assert "FENRIR_STALE_SECONDS" not in saved  # only secrets are imported
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_import_with_no_repo_secrets_is_409(env, repo_env, monkeypatch):
    client, env_file, _ = env
    repo_env.write_text("FENRIR_NT_TOKEN=\n")
    assert client.post(IMPORT, headers=WRITE).status_code == 409
    repo_env.write_text("FENRIR_NT_TOKEN=x\nFENRIR_LSE_API_KEY=y\n")
    r = client.post(IMPORT, headers=WRITE)  # both already set: never overwritten
    assert r.status_code == 409 and r.json()["detail"].endswith("already set in the secrets file")
    env_file.write_bytes(b"")
    monkeypatch.delenv("FENRIR_BIND_ALL")  # natively the repo .env is the secrets file
    assert client.post(IMPORT, headers=WRITE).status_code == 409
    assert env_file.read_bytes() == b""


def test_import_rejects_a_mask_without_echoing_it(env, repo_env):
    client, env_file, _ = env
    env_file.write_bytes(b"")
    repo_env.write_text("FENRIR_LSE_API_KEY=••••leak\n")
    r = client.post(IMPORT, headers=WRITE)
    assert r.status_code == 422 and "leak" not in r.text
    assert r.json()["errors"] == [
        {"loc": ["lse_api_key"], "msg": "is a mask, not a secret; reload the page"}
    ]
    assert env_file.read_bytes() == b""


def test_import_needs_a_session_and_origin(env, repo_env):
    client, env_file, _ = env
    repo_env.write_text("FENRIR_NT_TOKEN=x\n")
    assert client.post(IMPORT).status_code == 403
    client.cookies.clear()
    assert client.post(IMPORT, headers=WRITE).status_code == 401
    assert env_file.read_bytes() == ENV.encode()


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


def test_a_fifo_at_the_repo_env_is_no_secrets(env, tmp_path, monkeypatch):
    client, _, _ = env
    repo_env = tmp_path / "repo.env"
    os.mkfifo(repo_env)  # a blocking open would hang the request
    monkeypatch.setattr(routes, "REPO_ENV", repo_env)
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    assert client.get("/api/settings").json()["repo_env_secrets"] is False


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
