from pathlib import Path

import pytest

from fenrir.settings import Settings, load_settings, parse_env_text, parse_settings


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


def test_load_settings_environ_overrides_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FENRIR_NT_TOKEN=from-file\nFENRIR_STALE_SECONDS=10\n")
    monkeypatch.setenv("FENRIR_NT_TOKEN", "from-env")
    s = load_settings(env_file)
    assert s.nt_token == "from-env"
    assert s.stale_seconds == 10


def test_load_settings_missing_file_uses_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("FENRIR_NT_TOKEN", raising=False)
    assert load_settings(tmp_path / "missing.env").nt_token == ""
