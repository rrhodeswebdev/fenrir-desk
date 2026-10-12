import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fenrir.server import pull
from fenrir.server.pull import PullRunner, pull_command, pull_env, pull_status
from fenrir.settings import Settings


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_pull_status_states():
    assert pull_status(None, None, None, [])["state"] == "idle"
    assert pull_status("t", None, None, [])["state"] == "running"
    assert pull_status("t", "u", 0, [])["state"] == "done"
    assert pull_status("t", "u", 2, ["x"]) == {
        "state": "error",
        "started_at": "t",
        "finished_at": "u",
        "returncode": 2,
        "log_tail": ["x"],
    }


def test_pull_command_runs_the_cli():
    cmd = pull_command()
    assert cmd[0] == sys.executable and cmd[-2:] == ["data", "pull"]
    out = subprocess.run([*cmd, "--help"], capture_output=True, text=True, check=True).stdout
    assert "--since" in out


def test_pull_command_ignores_modules_in_the_cwd(tmp_path):
    fake = tmp_path / "fenrir" / "cli"
    fake.mkdir(parents=True)
    (tmp_path / "fenrir" / "__init__.py").write_text("")  # regular packages, or the real one wins
    (fake / "__init__.py").write_text("")
    (fake / "main.py").write_text("print('PLANTED'); app = lambda: None")
    out = subprocess.run(
        [*pull_command(), "--help"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert "--since" in out and "PLANTED" not in out


def test_pull_env_passes_the_lse_key_but_not_the_nt_token_or_secrets_file():
    environ = {"PATH": "/bin", "FENRIR_NT_TOKEN": "nt", "FENRIR_ENV_FILE": "/var/lib/fenrir/.env"}
    env = pull_env(environ, Settings(lse_api_key="lse", data_dir=Path("d")))
    assert env["FENRIR_LSE_API_KEY"] == "lse" and env["FENRIR_DATA_DIR"] == "d"
    assert env["PATH"] == "/bin" and "FENRIR_NT_TOKEN" not in env
    # the child may run unprivileged: it must not read the root-only secrets file (#135)
    assert "FENRIR_ENV_FILE" not in env
    # an empty key in settings must not hide one already in the environment
    assert pull_env({"FENRIR_LSE_API_KEY": "env"}, Settings())["FENRIR_LSE_API_KEY"] == "env"


def test_successful_pull_reports_done():
    runner = PullRunner(py("print('pulled ES')"))
    assert runner.status()["state"] == "idle"
    assert runner.start() is True
    wait_for(lambda: runner.status()["state"] == "done")
    status = runner.status()
    assert status["log_tail"] == ["pulled ES"] and status["returncode"] == 0
    assert status["started_at"] <= status["finished_at"]


def test_failed_pull_reports_error_and_log():
    code = "import sys; print('starting'); sys.exit('FENRIR_LSE_API_KEY is not set')"
    # pull_env makes the child unbuffered, so stdout and stderr keep their order
    runner = PullRunner(py(code), pull_env(os.environ, Settings()))
    runner.start()
    wait_for(lambda: runner.status()["state"] == "error")
    assert runner.status()["log_tail"] == ["starting", "FENRIR_LSE_API_KEY is not set"]
    assert runner.status()["returncode"] == 1


def test_second_start_while_running_is_refused():
    runner = PullRunner(py("import time; time.sleep(1)"))
    assert runner.start() is True
    assert runner.start() is False
    assert runner.status()["state"] == "running" and runner.status()["finished_at"] is None
    wait_for(lambda: runner.status()["state"] == "done")
    assert runner.start() is True  # allowed again once finished
    assert runner.status()["state"] == "running"


def test_log_tail_is_last_20_lines():
    runner = PullRunner(py("for i in range(50): print(i)"))
    runner.start()
    wait_for(lambda: runner.status()["state"] == "done")
    assert runner.status()["log_tail"] == [str(i) for i in range(30, 50)]


def test_long_lines_are_clipped():
    runner = PullRunner(py("print('x' * 2000)"))
    runner.start()
    wait_for(lambda: runner.status()["state"] == "done")
    assert runner.status()["log_tail"] == ["x" * 500]


def test_child_gets_the_env_it_is_given():
    code = "import os; print(os.environ['FENRIR_LSE_API_KEY'])"
    runner = PullRunner(py(code), pull_env({}, Settings(lse_api_key="secret-key")))
    runner.start()
    wait_for(lambda: runner.status()["state"] == "done")
    assert runner.status()["log_tail"] == ["secret-key"]


def test_unisolated_image_refuses_to_start(monkeypatch):
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    runner = PullRunner(py("print('ran')"), settings=Settings())
    with pytest.raises(ValueError, match="FENRIR_PTY_USER is unset"):  # the shared check's reasons
        runner.start()
    assert runner.status()["state"] == "idle"


def test_image_pull_child_runs_as_the_server_user(monkeypatch):
    # never as FENRIR_PTY_USER: that user could read the LSE key from /proc/<pid>/environ
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.setenv("FENRIR_PTY_USER", "claude")
    monkeypatch.setattr(pull, "isolation_problems", lambda s: [])
    calls = []

    class Done:
        stdout = iter(())

        def wait(self) -> int:
            return 0

    def popen(cmd, **kwargs):
        calls.append(kwargs)
        return Done()

    monkeypatch.setattr(pull.subprocess, "Popen", popen)
    env = {"PATH": "/bin", "FENRIR_LSE_API_KEY": "k"}
    assert PullRunner(["pull"], env, settings=Settings()).start() is True
    [kwargs] = calls
    assert kwargs["env"] == env
    assert not {"user", "group", "extra_groups"} & kwargs.keys()


def test_isolation_problems_refuse_the_pull(monkeypatch):
    monkeypatch.setattr(pull, "isolation_problems", lambda s: ["first", "second"])
    runner = PullRunner(py("print('ran')"), settings=Settings())
    with pytest.raises(ValueError, match="refusing to pull: first; second"):
        runner.start()
    assert runner.status()["state"] == "idle"
