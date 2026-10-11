from pathlib import Path

import pytest

from fenrir.settings import (
    Settings,
    env_file_path,
    load_settings,
    parse_env_text,
    parse_settings,
)


def test_parse_settings_defaults_when_empty():
    assert parse_settings({}) == Settings()


def test_parse_settings_reads_every_key():
    s = parse_settings(
        {
            "FENRIR_LSE_API_KEY": "k",
            "FENRIR_NT_URL": "http://10.0.0.5:8123",
            "FENRIR_NT_TOKEN": "t",
            "FENRIR_HOLDOUT_MONTHS": "6",
            "FENRIR_SAFETY_MARGIN_PCT": "75.5",
            "FENRIR_STALE_SECONDS": "30",
            "FENRIR_HEARTBEAT_POLICY": "flatten",
            "FENRIR_DATA_DIR": "/tmp/d",
            "FENRIR_RULES_DIR": "/tmp/r",
            "FENRIR_STRATEGIES_DIR": "/tmp/s",
            "FENRIR_PRIVATE_DIR": "/tmp/p",
        }
    )
    assert s == Settings(
        lse_api_key="k",
        nt_url="http://10.0.0.5:8123",
        nt_token="t",
        holdout_months=6,
        safety_margin_pct=75.5,
        stale_seconds=30,
        heartbeat_policy="flatten",
        data_dir=Path("/tmp/d"),
        rules_dir=Path("/tmp/r"),
        strategies_dir=Path("/tmp/s"),
        private_dir=Path("/tmp/p"),
    )


def test_parse_settings_empty_value_means_default():
    assert parse_settings({"FENRIR_NT_URL": ""}).nt_url == Settings().nt_url


def test_parse_settings_bad_int_names_key():
    with pytest.raises(ValueError, match="FENRIR_HOLDOUT_MONTHS"):
        parse_settings({"FENRIR_HOLDOUT_MONTHS": "twelve"})


def test_parse_settings_bad_policy_names_key():
    with pytest.raises(ValueError, match="FENRIR_HEARTBEAT_POLICY"):
        parse_settings({"FENRIR_HEARTBEAT_POLICY": "panic"})


def test_parse_env_text_handles_comments_and_quotes():
    text = (
        '# comment\n\nFENRIR_NT_TOKEN="abc=def"\n'
        "  FENRIR_LSE_API_KEY = 'xyz'  \n"
        "export FENRIR_STALE_SECONDS=30\nnot a pair\n"
    )
    assert parse_env_text(text) == {
        "FENRIR_NT_TOKEN": "abc=def",
        "FENRIR_LSE_API_KEY": "xyz",
        "FENRIR_STALE_SECONDS": "30",
    }


def test_parse_env_text_inline_comments_and_mismatched_quotes():
    text = 'FENRIR_NT_URL=http://10.0.0.5:8123  # vm ip\nA=\'abc"\nB="x # y"\nC=a#b\n'
    assert parse_env_text(text) == {
        "FENRIR_NT_URL": "http://10.0.0.5:8123",
        "A": "'abc\"",
        "B": "x # y",
        "C": "a#b",
    }


def test_load_settings_environ_overrides_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FENRIR_NT_TOKEN=from-file\nFENRIR_STALE_SECONDS=10\n")
    monkeypatch.setenv("FENRIR_NT_TOKEN", "from-env")
    monkeypatch.delenv("FENRIR_STALE_SECONDS", raising=False)
    monkeypatch.setenv("FENRIR_ENV_FILE", str(env_file))
    s = load_settings()
    assert s.nt_token == "from-env"
    assert s.stale_seconds == 10


def test_load_settings_empty_environ_does_not_hide_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FENRIR_NT_TOKEN=from-file\n")
    monkeypatch.setenv("FENRIR_NT_TOKEN", "")
    monkeypatch.setenv("FENRIR_ENV_FILE", str(env_file))
    assert load_settings().nt_token == "from-file"


def test_load_settings_missing_file_uses_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("FENRIR_NT_TOKEN", raising=False)
    monkeypatch.setenv("FENRIR_ENV_FILE", str(tmp_path / "missing.env"))
    assert load_settings().nt_token == ""


@pytest.fixture
def repo_env(tmp_path, monkeypatch):
    """cwd holds a repo `.env` with a token; no env-only keys or token in the environment."""
    monkeypatch.chdir(tmp_path)
    for key in ("FENRIR_ENV_FILE", "FENRIR_BIND_ALL", "FENRIR_PTY_USER", "FENRIR_NT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("FENRIR_NT_TOKEN=from-repo\n")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("FENRIR_NT_TOKEN=from-secrets\n")
    return secrets


def test_natively_reads_the_repo_env_file(repo_env):
    assert load_settings().nt_token == "from-repo"


def test_natively_env_file_var_picks_the_file(repo_env, monkeypatch):
    monkeypatch.setenv("FENRIR_ENV_FILE", str(repo_env))
    assert load_settings().nt_token == "from-secrets"


def test_image_reads_only_the_env_file_var(repo_env, monkeypatch):
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.setenv("FENRIR_ENV_FILE", str(repo_env))
    assert load_settings().nt_token == "from-secrets"


@pytest.mark.parametrize("env_file", [None, "", "missing.env"])
def test_image_never_falls_back_to_the_repo_env(repo_env, monkeypatch, env_file):
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    if env_file is not None:
        monkeypatch.setenv("FENRIR_ENV_FILE", env_file)
    assert load_settings().nt_token == ""


def test_env_only_keys_in_a_file_are_ignored(repo_env):
    # a terminal-writable .env cannot redirect the secrets file or switch on image mode
    (repo_env.parent / ".env").write_text(
        f"FENRIR_ENV_FILE={repo_env}\nFENRIR_BIND_ALL=1\nFENRIR_PTY_USER=root\n"
    )
    assert load_settings().nt_token == ""


def test_env_file_path():
    assert env_file_path({}) == Path(".env")
    assert env_file_path({"FENRIR_ENV_FILE": "/s/.env"}) == Path("/s/.env")
    assert env_file_path({"FENRIR_BIND_ALL": "1"}) is None
    assert env_file_path({"FENRIR_BIND_ALL": "1", "FENRIR_ENV_FILE": "/s/.env"}) == Path("/s/.env")
    assert env_file_path({"FENRIR_BIND_ALL": "0"}) == Path(".env")
