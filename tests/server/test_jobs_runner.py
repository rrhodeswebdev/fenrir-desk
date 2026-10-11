import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from backtest_helpers import D1, D2, D3, bar, rules_data

from fenrir.backtest.grid import GridItem
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars
from fenrir.server.jobs import Job, JobItem, to_json
from fenrir.server.jobs_runner import CHILD, JobRunner, parse_event, strategy_user
from fenrir.settings import Settings

FAKE = (sys.executable, str(Path(__file__).with_name("fake_job_child.py")))
ORB = Path(__file__).parents[2] / "strategies" / "orb.py"


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def key(stem: str) -> str:
    return hashlib.sha256(stem.encode()).hexdigest()


def grid(tmp_path, *stems: str) -> tuple[GridItem, ...]:
    return tuple(GridItem(tmp_path / f"{s}.py", "lucid-flex-50k", "NQ") for s in stems)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def make(settings):
    runners = []

    def make(s: Settings = settings, child=FAKE) -> JobRunner:
        runners.append(JobRunner(s, child=child))
        return runners[-1]

    yield make
    for r in runners:
        r.stop()


def finished(runner: JobRunner, job_id: str) -> bool:
    return all(it.status in ("done", "error") for it in runner.get(job_id).items)


def test_submit_runs_items_and_persists_a_round_trip(make, settings, tmp_path):
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "orb"), True)
    wait_for(lambda: finished(runner, job.id))
    done = runner.get(job.id)
    assert (done.items[0].status, done.items[0].key) == ("done", key("orb"))
    assert done.items[0].progress == (200, 200)
    assert json.loads((settings.data_dir / "jobs" / f"{job.id}.json").read_text()) == json.loads(
        json.dumps(to_json(done))
    )
    again = make()
    again.start()
    assert again.get(job.id) == done
    assert again.jobs() == [done]


def test_failing_item_does_not_stop_the_queue(make, tmp_path):
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "bad", "orb"), True)
    wait_for(lambda: finished(runner, job.id))
    bad, good = runner.get(job.id).items
    assert (bad.status, bad.error) == ("error", "ValueError: boom")
    assert (good.status, good.key) == ("done", key("orb"))


def test_a_child_that_dies_errors_the_items_it_left(make, tmp_path):
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "crash", "orb"), True)
    wait_for(lambda: finished(runner, job.id))
    assert [it.error for it in runner.get(job.id).items] == [
        "backtest process exited with code 3"
    ] * 2


def test_restart_reruns_only_unfinished_items(make, settings, tmp_path):
    (a, b, c) = (str(tmp_path / f"{s}.py") for s in ("a", "b", "c"))
    job = Job(
        "j1",
        "2026-10-10T14:00:00.000Z",
        True,
        (
            JobItem(a, "lucid-flex-50k", "NQ", status="done", key="old"),
            JobItem(b, "lucid-flex-50k", "NQ", status="running", progress=(3, 9)),
            JobItem(c, "lucid-flex-50k", "NQ"),
        ),
    )
    (settings.data_dir / "jobs").mkdir(parents=True)
    (settings.data_dir / "jobs" / "j1.json").write_text(json.dumps(to_json(job)))
    runner = make()
    runner.start()
    wait_for(lambda: finished(runner, "j1"))
    assert [it.key for it in runner.get("j1").items] == ["old", key("b"), key("c")]


def test_a_corrupt_job_file_is_skipped_and_logged(make, settings, caplog):
    jobs = settings.data_dir / "jobs"
    jobs.mkdir(parents=True)
    good = Job("good", "2026-10-10T14:00:00.000Z", True, ())
    (jobs / "good.json").write_text(json.dumps(to_json(good)))
    (jobs / "half.json").write_text('{"id": ')
    (jobs / "fields.json").write_text(json.dumps({**to_json(good), "id": "fields", "items": [{}]}))
    (jobs / "renamed.json").write_text(json.dumps(to_json(good)))  # id must match the file name
    runner = make()
    runner.start()
    assert runner.jobs() == [good]
    assert sorted(r.message.split(":")[0] for r in caplog.records) == [
        f"skipping job file {jobs / n}.json" for n in ("fields", "half", "renamed")
    ]


def test_listeners_see_pending_running_done_and_throttled_progress(make, tmp_path):
    runner = make()
    seen: list[dict] = []
    runner.add_listener(seen.append)
    runner.start()
    job = runner.submit(grid(tmp_path, "orb"), True)
    wait_for(lambda: finished(runner, job.id))
    items = [p["items"][0] for p in seen if p["id"] == job.id]
    statuses = [it["status"] for it in items]
    assert statuses[0] == "pending" and statuses[-1] == "done"
    assert set(statuses) == {"pending", "running", "done"}
    progress = [it["progress"] for it in items if it["status"] == "running"][1:]
    # 200 child updates: one when the total appears, then one per whole percent
    assert len(progress) == 101 and progress[-1] == (200, 200)
    runner.remove_listener(seen.append)
    runner.submit(grid(tmp_path, "orb"), True)
    count = len(seen)
    time.sleep(0.2)
    assert len(seen) == count


def test_a_raising_listener_does_not_stop_the_runner(make, tmp_path):
    runner = make()

    def boom(_job: dict) -> None:
        raise RuntimeError("listener")

    runner.add_listener(boom)
    runner.start()
    job = runner.submit(grid(tmp_path, "orb"), True)
    wait_for(lambda: finished(runner, job.id))


def test_jobs_are_newest_first(make, tmp_path):
    runner = make()
    first = runner.submit(grid(tmp_path, "orb"), True)
    second = runner.submit(grid(tmp_path, "orb"), True)
    assert [j.id for j in runner.jobs()] == [second.id, first.id]


def test_the_child_env_holds_no_secrets(make, tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIR_NT_TOKEN", "secret")
    monkeypatch.setenv("FENRIR_LSE_API_KEY", "secret")
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "env"), True)
    wait_for(lambda: finished(runner, job.id))
    assert runner.get(job.id).items[0].error == (
        "FENRIR_DATA_DIR FENRIR_HOLDOUT_MONTHS FENRIR_RULES_DIR FENRIR_STRATEGIES_DIR"
    )


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_child_runs_in_its_own_session_and_stop_kills_its_group(make, tmp_path):
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "hang"), True)
    info_file = tmp_path / "hang.json"
    wait_for(info_file.exists)
    info = json.loads(info_file.read_text())
    assert info["own_session"]
    runner.stop()
    wait_for(lambda: gone(info["sleeper"]))
    # left running, so the next start re-runs it
    assert runner.get(job.id).items[0].status == "running"


def test_isolation_refusal_errors_the_items(make, tmp_path, monkeypatch):
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    runner = make()
    runner.start()
    job = runner.submit(grid(tmp_path, "orb"), True)
    wait_for(lambda: finished(runner, job.id))
    assert runner.get(job.id).items[0].error.startswith("ValueError: refusing to run strategy")


def test_strategy_user():
    assert strategy_user({}) is None
    assert strategy_user({"FENRIR_PTY_USER": "claude"}) == "claude"
    for env in (
        {"FENRIR_BIND_ALL": "1"},
        {"FENRIR_BIND_ALL": "1", "FENRIR_PTY_USER": "root"},
        {"FENRIR_BIND_ALL": "1", "FENRIR_PTY_USER": "no-such-user-here"},
    ):
        with pytest.raises(ValueError, match="refusing to run strategy"):
            strategy_user(env)


K = "a" * 64


@pytest.mark.parametrize(
    ("line", "event"),
    [
        ('{"i": 0, "status": "running"}', (0, {"status": "running"})),
        ('{"i": 1, "progress": [3, 9]}', (1, {"progress": (3, 9)})),
        (f'{{"i": 0, "status": "done", "key": "{K}"}}', (0, {"status": "done", "key": K})),
        ('{"i": 0, "status": "error", "error": "E: x"}', (0, {"status": "error", "error": "E: x"})),
    ],
)
def test_parse_event(line, event):
    assert parse_event(line, (0, 1)) == event


@pytest.mark.parametrize(
    "line",
    [
        "print output",
        "[]",
        '{"i": 2, "status": "running"}',  # not an item this child was given
        '{"i": 0, "status": "pending"}',
        '{"i": 0, "status": "running", "key": "x"}',
        '{"i": 0, "status": "done", "key": "../../x"}',
        '{"i": 0, "progress": [1]}',
        '{"i": "0", "status": "running"}',
    ],
)
def test_parse_event_rejects_anything_else(line):
    with pytest.raises(ValueError):
        parse_event(line, (0, 1))


def test_real_job_produces_a_cached_report(make, tmp_path):
    s = replace(
        Settings(),
        data_dir=tmp_path / "data",
        rules_dir=tmp_path / "rules",
        holdout_months=0,
    )
    (s.rules_dir / "lucid").mkdir(parents=True)
    rules = {**rules_data(), "account": "flex-50k"}
    (s.rules_dir / "lucid" / "flex-50k.yaml").write_text(yaml.safe_dump(rules))
    code = front_month(load_instruments(), "NQ", D1)
    bars = [
        replace(bar(f"09:{m:02d}", p, p + 2, p - 1, p + 1, day), contract=code)
        for day in (D1, D2, D3)
        for m, p in ((m, 20000.0 + 3 * m) for m in range(30, 60))
    ]
    write_bars(s.data_dir, "NQ", code, bars)
    runner = make(s, child=CHILD)
    runner.start()
    job = runner.submit(
        (GridItem(ORB, "lucid-flex-50k", "MES"), GridItem(ORB, "lucid-flex-50k", "MNQ")), False
    )
    wait_for(lambda: finished(runner, job.id), timeout=120)
    wrong, good = runner.get(job.id).items
    assert wrong.error.startswith("ValueError: MES trades ES prices")
    assert good.status == "done" and good.progress[0] == good.progress[1] == 3
    report = json.loads((s.data_dir / "results" / f"{good.key}.json").read_text())
    assert report["periods"]["tuning"]["attempts"] == 3
