import errno
import os
import socket
import stat

import pytest
from typer.testing import CliRunner

from fenrir.cli import serve as serve_mod
from fenrir.cli.main import app


def free_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


@pytest.fixture
def port():
    return free_port()


@pytest.fixture
def runs(monkeypatch, tmp_path):
    """Run in tmp_path (no repo .env); record each server run (app, bound addresses), not serve."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    monkeypatch.delenv("FENRIR_PRIVATE_DIR", raising=False)
    monkeypatch.delenv("FENRIR_BIND_ALL", raising=False)
    monkeypatch.delenv("FENRIR_ENV_FILE", raising=False)
    calls = []

    def run(self, sockets=None):
        calls.append((self.config.app, [s.getsockname()[:2] for s in sockets]))
        for s in sockets:
            s.close()

    monkeypatch.setattr(serve_mod.uvicorn.Server, "run", run)
    return calls


def built(path):
    path.mkdir(parents=True)
    (path / "index.html").write_text("<!doctype html>")
    return path


def invoke(*args, env=None):
    return CliRunner().invoke(app, ["serve", "--port", str(free_port()), *args], env=env)


def launch_code(output):
    return output.split("/launch?code=")[1].split()[0]


def test_binds_both_loopback_addresses_only(runs, tmp_path, port):
    result = invoke("--port", str(port), "--static-dir", str(built(tmp_path / "dist")))
    assert result.exit_code == 0, result.output
    assert [addrs for _, addrs in runs] == [[("127.0.0.1", port), ("::1", port)]]
    assert "UI not built" not in result.output


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1"])
def test_bind_hosts_is_loopback_unless_bind_all_is_exactly_1(value):
    environ = {} if value is None else {"FENRIR_BIND_ALL": value}
    assert serve_mod.bind_hosts(environ) == ("127.0.0.1", "::1")


def test_bind_hosts_is_all_ipv4_for_bind_all_1():
    assert serve_mod.bind_hosts({"FENRIR_BIND_ALL": "1"}) == ("0.0.0.0",)


def test_bind_all_binds_0000_and_still_prints_the_localhost_link(runs, monkeypatch, port):
    asked = []
    create_server = socket.create_server

    def record(address, **kw):  # bind loopback for real: no 0.0.0.0 listener on the dev box
        asked.append(address)
        return create_server(("127.0.0.1", address[1]), **kw)

    monkeypatch.setattr(serve_mod.socket, "create_server", record)
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    result = invoke("--port", str(port))
    assert result.exit_code == 0, result.output
    assert [a for a in asked if a[1] == port] == [("0.0.0.0", port)]  # invoke's free_port asks 0
    assert len(runs) == 1
    assert f"Open http://localhost:{port}/launch?code=" in result.output


def test_no_ipv6_loopback_falls_back_to_127_with_a_warning(runs, monkeypatch, port):
    create_server = socket.create_server

    def no_v6(address, **kw):
        if address[0] == "::1":
            raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
        return create_server(address, **kw)

    monkeypatch.setattr(serve_mod.socket, "create_server", no_v6)
    result = invoke("--port", str(port))
    assert result.exit_code == 0, result.output
    assert "no IPv6 loopback" in result.output
    assert [addrs for _, addrs in runs] == [[("127.0.0.1", port)]]


@pytest.mark.parametrize("family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_port_taken_on_either_address_exits_1_before_the_link(runs, port, family, host):
    with socket.create_server((host, port), family=family):
        result = invoke("--port", str(port))
    assert result.exit_code == 1
    assert f"cannot listen on port {port}" in result.output
    assert "launch?code=" not in result.output
    assert runs == []


def test_prints_launch_url_with_a_fresh_code_each_start(runs, port):
    first, second = invoke("--port", str(port)), invoke("--port", str(port))
    assert f"Open http://localhost:{port}/launch?code=" in first.output
    assert launch_code(first.output) != launch_code(second.output)
    assert runs[0][0].state.allowed_origins == frozenset(
        {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    )


def test_dev_prints_the_vite_url(runs):
    result = invoke("--dev")
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


@pytest.fixture
def key_path(tmp_path):
    (tmp_path / "data" / "private").mkdir(parents=True)
    return tmp_path / "data" / "private" / "session.key"


def refused(message):
    result = invoke()
    assert result.exit_code == 1
    assert message in result.output
    assert "launch?code=" not in result.output


def test_short_session_key_is_refused(runs, key_path):
    key_path.write_text("x" * 31)
    key_path.chmod(0o600)
    refused("under 32 characters")
    assert runs == []


def test_loose_session_key_mode_is_refused(runs, key_path):
    key_path.write_text("x" * 43)
    key_path.chmod(0o644)
    refused("chmod 600 it or delete it")
    assert runs == []


def test_symlinked_session_key_is_refused(runs, key_path, tmp_path):
    (tmp_path / "elsewhere").write_text("x" * 43)
    (tmp_path / "elsewhere").chmod(0o600)
    key_path.symlink_to(tmp_path / "elsewhere")
    refused("is a symlink")
    assert runs == []


def test_session_key_owned_by_another_user_is_refused(runs, key_path, monkeypatch):
    key_path.write_text("x" * 43)
    key_path.chmod(0o600)
    euid = os.geteuid()
    monkeypatch.setattr(serve_mod.os, "geteuid", lambda: euid + 1)
    refused("owned by another user")
    assert runs == []


def test_existing_0600_key_of_32_chars_is_used(runs, key_path):
    key_path.write_text("k" * 32 + "\n")
    key_path.chmod(0o600)
    assert invoke().exit_code == 0
    assert runs[0][0].state.session_key == "k" * 32


def test_without_build_prints_ui_not_built_and_serves_the_api(runs, tmp_path):
    result = invoke("--static-dir", str(tmp_path / "none"))
    assert result.exit_code == 0, result.output
    assert "UI not built" in result.output
    assert len(runs) == 1


def test_notice_when_terminal_not_isolated(runs, monkeypatch):
    assert "not isolated" in invoke().output
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    assert "not isolated" not in invoke().output


def test_logs_each_isolation_problem_in_the_image(runs, monkeypatch):
    assert "not isolated, terminal" not in invoke().output
    monkeypatch.setattr(serve_mod, "isolation_problems", lambda s: ["first", "second"])
    result = invoke()
    assert result.exit_code == 0, result.output
    assert "terminal and strategies disabled: first" in result.output
    assert "terminal and strategies disabled: second" in result.output


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
