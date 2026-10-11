import stat

import pytest
from typer.testing import CliRunner

from fenrir.cli import serve as serve_mod
from fenrir.cli.main import app


@pytest.fixture
def runs(monkeypatch, tmp_path):
    """Run in tmp_path (no repo .env); record each uvicorn.run call instead of serving."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    monkeypatch.delenv("FENRIR_PRIVATE_DIR", raising=False)
    calls = []
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda a, **kw: calls.append((a, kw)))
    return calls


def built(path):
    path.mkdir(parents=True)
    (path / "index.html").write_text("<!doctype html>")
    return path


def invoke(*args, env=None):
    return CliRunner().invoke(app, ["serve", *args], env=env)


def launch_code(output):
    return output.split("/launch?code=")[1].split()[0]


def test_binds_loopback_only(runs, tmp_path):
    result = invoke("--port", "8123", "--static-dir", str(built(tmp_path / "dist")))
    assert result.exit_code == 0, result.output
    assert [kw for _, kw in runs] == [{"host": "127.0.0.1", "port": 8123}]
    assert "UI not built" not in result.output


def test_prints_launch_url_with_a_fresh_code_each_start(runs):
    first, second = invoke("--port", "8123"), invoke("--port", "8123")
    assert "Open http://localhost:8123/launch?code=" in first.output
    assert launch_code(first.output) != launch_code(second.output)
    assert runs[0][0].state.allowed_origins == frozenset(
        {"http://localhost:8123", "http://127.0.0.1:8123"}
    )


def test_dev_prints_the_vite_url(runs):
    result = invoke("--port", "8123", "--dev")
    assert "Open http://localhost:5173/launch?code=" in result.output
    assert "http://localhost:5173" in runs[0][0].state.allowed_origins


def test_session_key_created_0600_and_reused(runs, tmp_path):
    env = {"FENRIR_PRIVATE_DIR": str(tmp_path / "priv" / "ate")}
    invoke(env=env)
    key = tmp_path / "priv" / "ate" / "session.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    invoke(env=env)
    assert runs[0][0].state.session_key == runs[1][0].state.session_key == key.read_text()
    assert len(key.read_text()) >= 43  # token_urlsafe(32)


def test_default_private_dir_is_data_private(runs, tmp_path):
    invoke()
    assert (tmp_path / "data" / "private" / "session.key").is_file()


def test_empty_session_key_is_refused(runs, tmp_path):
    (tmp_path / "data" / "private").mkdir(parents=True)
    (tmp_path / "data" / "private" / "session.key").write_text("")
    result = invoke()
    assert result.exit_code == 1
    assert "session.key is empty" in result.output
    assert runs == []


def test_without_build_prints_ui_not_built_and_serves_the_api(runs, tmp_path):
    result = invoke("--static-dir", str(tmp_path / "none"))
    assert result.exit_code == 0, result.output
    assert "UI not built" in result.output
    assert len(runs) == 1


def test_notice_when_terminal_not_isolated(runs, monkeypatch):
    assert "not isolated" in invoke().output
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    assert "not isolated" not in invoke().output


@pytest.mark.parametrize(
    "static, private",
    [
        (".", "."),  # equals private_dir
        (".", "data/private"),  # contains private_dir
        ("site", "data/private"),  # holds a nested .env (made below)
    ],
)
def test_refuses_static_dir_exposing_secrets(runs, tmp_path, static, private):
    built(tmp_path / "site")
    (tmp_path / "site" / "assets").mkdir()
    (tmp_path / "site" / "assets" / ".env").write_text("FENRIR_LSE_API_KEY=x")
    (tmp_path / "index.html").write_text("<!doctype html>")
    result = invoke("--static-dir", static, env={"FENRIR_PRIVATE_DIR": private})
    assert result.exit_code == 1
    assert "refusing to serve" in result.output
    assert runs == []
