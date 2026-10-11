import json
from pathlib import Path

from fenrir.backtest.grid import GridItem
from fenrir.server.jobs import (
    Job,
    JobItem,
    from_json,
    grid_item,
    new_job,
    progress_step,
    restart,
    to_json,
    unfinished,
    update_item,
)

GRID = (
    GridItem(Path("strategies/orb.py"), "lucid-flex-50k", "NQ"),
    GridItem(Path("strategies/orb.py"), "lucid-flex-50k", "MNQ"),
)


def make_job() -> Job:
    return new_job("j1", "2026-10-10T14:00:00Z", GRID, True)


def test_new_job_starts_all_items_pending():
    job = make_job()
    assert [i.status for i in job.items] == ["pending", "pending"]
    assert job.items[1] == JobItem("strategies/orb.py", "lucid-flex-50k", "MNQ")


def test_update_item_changes_only_that_item():
    job = update_item(make_job(), 1, status="done", key="abc")
    assert job.items[0].status == "pending"
    assert (job.items[1].status, job.items[1].key) == ("done", "abc")


def test_unfinished_lists_pending_and_running():
    job = update_item(update_item(make_job(), 0, status="done"), 1, status="running")
    assert unfinished(job) == (1,)


def test_restart_turns_running_back_to_pending_and_clears_progress():
    job = update_item(make_job(), 0, status="running", progress=(5, 10))
    job = update_item(job, 1, status="done", key="k")
    again = restart(job)
    assert again.items[0] == JobItem("strategies/orb.py", "lucid-flex-50k", "NQ")
    assert again.items[1].status == "done"


def test_grid_item_round_trips():
    assert grid_item(make_job().items[0]) == GRID[0]


def test_json_round_trip_matches_contract_shape():
    job = update_item(make_job(), 0, progress=(3, 7))
    data = json.loads(json.dumps(to_json(job)))
    assert data == {
        "id": "j1",
        "created_at": "2026-10-10T14:00:00Z",
        "include_holdout": True,
        "items": [
            {
                "strategy": "strategies/orb.py",
                "account": "lucid-flex-50k",
                "contract": "NQ",
                "status": "pending",
                "key": None,
                "error": None,
                "progress": [3, 7],
            },
            {
                "strategy": "strategies/orb.py",
                "account": "lucid-flex-50k",
                "contract": "MNQ",
                "status": "pending",
                "key": None,
                "error": None,
                "progress": [0, 0],
            },
        ],
    }
    assert from_json(data) == job


def test_progress_step_only_on_whole_percent():
    assert progress_step((0, 0), (1, 1000)) is True  # total became known
    assert progress_step((1, 1000), (5, 1000)) is False  # still 0%
    assert progress_step((5, 1000), (10, 1000)) is True  # 0% -> 1%
    sent = sum(progress_step((d - 1, 5000), (d, 5000)) for d in range(1, 5001))
    assert sent == 100
