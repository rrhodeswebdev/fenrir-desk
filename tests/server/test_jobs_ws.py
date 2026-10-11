import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fenrir.backtest.grid import GridItem
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.jobs_runner import JobRunner
from fenrir.settings import Settings

FAKE = (sys.executable, str(Path(__file__).with_name("fake_job_child.py")))
KEY = "k" * 43
BASE = "http://localhost:8000"
WS = "ws://localhost:8000/ws/jobs"
GOOD = {"cookie": f"{SESSION_COOKIE}={KEY}", "origin": BASE}


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def app_runner(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), KEY, "c" * 22, None, port=8000)
    app.state.jobs = runner = JobRunner(app.state.settings, child=FAKE, secret=KEY)
    return app, runner


def _grid(tmp_path) -> tuple[GridItem, ...]:
    return (GridItem(tmp_path / "orb.py", "lucid-flex-50k", "NQ"),)


def test_sends_current_jobs_then_every_change(app_runner, tmp_path):
    app, runner = app_runner
    with TestClient(app, base_url=BASE) as client:
        old = runner.submit(_grid(tmp_path), True)
        with client.websocket_connect(WS, headers=GOOD) as ws:
            assert ws.receive_json()["id"] == old.id  # the current jobs come first
            new = runner.submit(_grid(tmp_path), False)  # from this thread, not the loop's
            seen = []
            while not seen or seen[-1]["items"][0]["status"] != "done":
                if (job := ws.receive_json())["id"] == new.id:
                    seen.append(job)
            assert seen[0]["items"][0]["status"] == "pending"
            assert seen[-1]["include_holdout"] is False
            assert seen[-1]["items"][0]["progress"] == [200, 200]
            ws.close()  # a closed tab: dropped without waiting for another change
            wait_for(lambda: not runner._listeners)


class BurstRunner:
    """Fires a burst of changes for one job before the socket can send any of them."""

    def __init__(self) -> None:
        self.listeners: list = []

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def add_listener(self, fn) -> None:
        self.listeners.append(fn)

    def remove_listener(self, fn) -> None:
        self.listeners.remove(fn)

    def jobs(self) -> list:
        for n in range(1000):  # on the loop thread: none is sent before the handler yields
            self.listeners[0]({"id": "j", "n": n})
        return []


def test_a_burst_of_changes_sends_only_the_latest(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), KEY, "c" * 22, None, port=8000)
    app.state.jobs = runner = BurstRunner()
    with TestClient(app, base_url=BASE) as client:
        with client.websocket_connect(WS, headers=GOOD) as ws:
            assert ws.receive_json() == {"id": "j", "n": 999}
            runner.listeners[0]({"id": "j", "n": 1000})  # from this thread
            assert ws.receive_json() == {"id": "j", "n": 1000}  # not a stale 1..998
            ws.close()
            wait_for(lambda: not runner.listeners)


@pytest.mark.parametrize(
    "headers",
    [
        {"cookie": f"{SESSION_COOKIE}=wrong", "origin": BASE},
        {"cookie": f"{SESSION_COOKIE}={KEY}", "origin": "http://evil.example"},
        {"cookie": f"{SESSION_COOKIE}={KEY}"},
        {"origin": BASE},
    ],
    ids=["bad-cookie", "foreign-origin", "no-origin", "no-cookie"],
)
def test_refused_before_accept(app_runner, headers):
    app, runner = app_runner
    with TestClient(app, base_url=BASE) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS, headers=headers):
                pass
        assert runner._listeners == []
