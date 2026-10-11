import os
import subprocess
import sys
import time

from fenrir.server.pull import PullRunner, pull_command, pull_env, pull_status


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


def test_pull_env_passes_the_lse_key_but_not_the_nt_token():
    env = pull_env({"PATH": "/bin", "FENRIR_NT_TOKEN": "nt", "FENRIR_DATA_DIR": "d"}, "lse")
    assert env["FENRIR_LSE_API_KEY"] == "lse" and env["FENRIR_DATA_DIR"] == "d"
    assert env["PATH"] == "/bin" and "FENRIR_NT_TOKEN" not in env
    # an empty key in settings must not hide one already in the environment
    assert pull_env({"FENRIR_LSE_API_KEY": "env"}, "")["FENRIR_LSE_API_KEY"] == "env"


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
    runner = PullRunner(py(code), pull_env(os.environ, ""))
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


def test_child_gets_the_env_it_is_given():
    code = "import os; print(os.environ['FENRIR_LSE_API_KEY'])"
    runner = PullRunner(py(code), pull_env({}, "secret-key"))
    runner.start()
    wait_for(lambda: runner.status()["state"] == "done")
    assert runner.status()["log_tail"] == ["secret-key"]
