# Backtest Jobs and Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run strategy × account × contract grids from the UI as background jobs with live progress, compare results by account or by strategy, open a full report page, and show the latest report next to the Workbench terminal.

**Architecture:** Job state is pure (`fenrir.server.jobs`: frozen `Job`/`JobItem` plus transition functions). A thin shell (`fenrir.server.jobs_runner.JobRunner`) persists jobs to `data/jobs/<id>.json`, runs items one at a time on a worker thread through plan 10's `cached_run` (which already uses every CPU core per item), and pushes job changes to WebSocket listeners. Ranking for the comparison table is a pure function in `fenrir.backtest.compare` that sorts by tuning results only. The React side renders the server's grouped rows, the report JSON, and equity curves with Lightweight Charts.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, pytest; React 19, TanStack Router + Query, `lightweight-charts` v5, Playwright. UI: Tailwind CSS v4 utility classes + shadcn/ui components (`@/components/ui/*`, set up in plan 12; add missing ones with `npx shadcn@latest add`); no custom CSS classes or inline styles except runtime-computed values.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§6 Grids and cache, §6 Report, §10 Backtests and Workbench rows)

**Index:** `docs/superpowers/plans/README.md` (cross-plan contracts: plan 10 `cached_run`/`expand_grid`/`list_reports`, `Report` JSON, plan 11 server conventions, plan 12 web conventions, plan 14 jobs API)

## Global Constraints

- Functional core, imperative shell: core logic is pure functions over `@dataclass(frozen=True, slots=True)` values and tuples; I/O only in thin shells; FastAPI handlers only parse and forward.
- Every `/api` router is registered with `dependencies=[Depends(require_token)]`; every WebSocket calls `check_ws(ws)` before `accept()` and closes with code 4401 on failure.
- Ranking sorts by tuning-period results only; held-out results are shown but never used to sort (spec §6).
- UI runs produce the full report (tuning + held-out): the UI always sends `include_holdout: true` (spec §6 Hold-out).
- If the server restarts mid-run, unfinished pairs re-run next time (spec §6).
- React: function components + hooks; server state only in TanStack Query; query keys `['<area>', ...args]`.
- Charts use TradingView Lightweight Charts (spec §10).
- Commits: Conventional Commits, scope `server`, `backtest` or `web`; every message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Server restarts mid-job** — items that were `pending` or `running` re-run on the next `fenrir serve`; `done` items are not re-run and keep their `key`. Test: `test_restart_reruns_only_unfinished_items` (Task 2).
2. **One pair in a grid fails** (syntax error in a strategy, missing data) — that item shows `error` with the message, and the remaining items still run. Test: `test_failing_item_does_not_stop_the_queue` (Task 2).
3. **Request names a strategy outside `strategies/`** (e.g. `../../tmp/evil.py`) — rejected with 422 before anything is loaded, because loading a strategy executes it. Test: `test_create_job_rejects_unknown_strategy_path` (Task 4).
4. **Held-out results better than tuning** — ranking must not move. Test: `test_holdout_never_changes_order` (Task 3).
5. **Progress floods** — `on_progress` fires once per attempt (thousands); the UI must get at most ~100 messages per item. Test: `test_progress_step_only_on_whole_percent` (Task 1).

---

### Task 1: Pure job state

**Files:**
- Create: `src/fenrir/server/jobs.py`
- Modify: `src/fenrir/server/CLAUDE.md`
- Test: `tests/server/test_jobs.py`

**Interfaces:**
- Consumes: `GridItem(strategy_path: Path, account_id: str, contract: str)` from `fenrir.backtest.grid` (plan 10).
- Produces:
  - `Status = Literal["pending", "running", "done", "error"]`
  - `JobItem(strategy: str, account: str, contract: str, status: Status = "pending", key: str | None = None, error: str | None = None, progress: tuple[int, int] = (0, 0))`
  - `Job(id: str, created_at: str, include_holdout: bool, items: tuple[JobItem, ...])`
  - `new_job(job_id: str, created_at: str, grid: Sequence[GridItem], include_holdout: bool) -> Job`
  - `update_item(job: Job, index: int, **changes: Any) -> Job`
  - `unfinished(job: Job) -> tuple[int, ...]`
  - `restart(job: Job) -> Job`
  - `grid_item(item: JobItem) -> GridItem`
  - `progress_step(old: tuple[int, int], new: tuple[int, int]) -> bool`
  - `to_json(job: Job) -> dict[str, Any]`, `from_json(data: Mapping[str, Any]) -> Job`

- [ ] **Step 1: Write the failing tests**

`tests/server/test_jobs.py`:

```python
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
    data = to_json(job)
    assert data["items"][0] == {
        "strategy": "strategies/orb.py",
        "account": "lucid-flex-50k",
        "contract": "NQ",
        "status": "pending",
        "key": None,
        "error": None,
        "progress": (3, 7),
    }
    assert from_json(data) == job


def test_progress_step_only_on_whole_percent():
    assert progress_step((0, 0), (1, 1000)) is True  # total became known
    assert progress_step((1, 1000), (5, 1000)) is False  # still 0%
    assert progress_step((5, 1000), (10, 1000)) is True  # 0% -> 1%
    sent = sum(progress_step((d - 1, 5000), (d, 5000)) for d in range(1, 5001))
    assert sent == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_jobs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.server.jobs'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/server/jobs.py`:

```python
"""Pure backtest-job state. `jobs_runner.py` does the files, threads and sockets."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from fenrir.backtest.grid import GridItem

Status = Literal["pending", "running", "done", "error"]


@dataclass(frozen=True, slots=True)
class JobItem:
    strategy: str
    account: str
    contract: str
    status: Status = "pending"
    key: str | None = None
    error: str | None = None
    progress: tuple[int, int] = (0, 0)


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    created_at: str
    include_holdout: bool
    items: tuple[JobItem, ...]


def new_job(job_id: str, created_at: str, grid: Sequence[GridItem], include_holdout: bool) -> Job:
    items = tuple(JobItem(str(g.strategy_path), g.account_id, g.contract) for g in grid)
    return Job(job_id, created_at, include_holdout, items)


def update_item(job: Job, index: int, **changes: Any) -> Job:
    items = tuple(replace(it, **changes) if i == index else it for i, it in enumerate(job.items))
    return replace(job, items=items)


def unfinished(job: Job) -> tuple[int, ...]:
    return tuple(i for i, it in enumerate(job.items) if it.status in ("pending", "running"))


def restart(job: Job) -> Job:
    """After a server restart nothing is running, so running items go back to pending."""
    items = tuple(
        replace(it, status="pending", progress=(0, 0)) if it.status == "running" else it
        for it in job.items
    )
    return replace(job, items=items)


def grid_item(item: JobItem) -> GridItem:
    return GridItem(Path(item.strategy), item.account, item.contract)


def _pct(p: tuple[int, int]) -> int:
    return p[0] * 100 // p[1] if p[1] else 0


def progress_step(old: tuple[int, int], new: tuple[int, int]) -> bool:
    """True when the whole-percent value or the total changed: at most ~100 updates per item."""
    return old[1] != new[1] or _pct(old) != _pct(new)


def to_json(job: Job) -> dict[str, Any]:
    return asdict(job)


def from_json(data: Mapping[str, Any]) -> Job:
    items = tuple(JobItem(**{**i, "progress": tuple(i["progress"])}) for i in data["items"])
    return Job(data["id"], data["created_at"], data["include_holdout"], items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/server/test_jobs.py -q`
Expected: `7 passed`

- [ ] **Step 5: Update the directory context file**

Append to the bullet list in `src/fenrir/server/CLAUDE.md`:

```markdown
- Backtest jobs: `jobs.py` (pure job state), `jobs_runner.py` (`JobRunner`: `data/jobs/*.json`, worker thread, WebSocket listeners; `fenrir serve` calls `start()` to resume unfinished items), `routes/backtests.py`.
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/server/jobs.py src/fenrir/server/CLAUDE.md tests/server/test_jobs.py
git commit -m "feat(server): add pure backtest job state

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Job runner shell (persistence, worker, resume, listeners)

**Files:**
- Create: `src/fenrir/server/jobs_runner.py`
- Test: `tests/server/test_jobs_runner.py`

**Interfaces:**
- Consumes: Task 1 functions; `cached_run(item, settings, *, include_holdout, on_progress) -> Report` from `fenrir.backtest.cache` (plan 10); `Settings` (plan 01).
- Produces: `JobRunner(settings: Settings, run: Callable[..., Report] = cached_run)` with `start() -> None` (loads `data/jobs/*.json`, re-queues unfinished items, starts the worker; idempotent), `submit(grid: Sequence[GridItem], include_holdout: bool) -> Job`, `jobs() -> list[Job]` (newest first), `get(job_id: str) -> Job | None`, `subscribe(loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict]`, `unsubscribe(q: asyncio.Queue[dict]) -> None`. Each listener receives `to_json(job)` on every change.

- [ ] **Step 1: Write the failing tests**

`tests/server/test_jobs_runner.py`:

```python
import asyncio
import json
import time
from pathlib import Path

from fenrir.backtest.grid import GridItem
from fenrir.server.jobs import Job, JobItem, from_json, to_json
from fenrir.server.jobs_runner import JobRunner
from fenrir.settings import Settings


def wait_for(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def fake_run(item, settings, *, include_holdout, on_progress=None):
    if on_progress:
        on_progress(1, 2)
        on_progress(2, 2)
    if "bad" in str(item.strategy_path):
        raise ValueError("boom")
    return {"key": f"key-{item.strategy_path.stem}-{item.contract}"}


GRID = (
    GridItem(Path("strategies/bad.py"), "lucid-flex-50k", "NQ"),
    GridItem(Path("strategies/orb.py"), "lucid-flex-50k", "NQ"),
)


def test_submit_runs_items_and_persists(tmp_path):
    runner = JobRunner(Settings(data_dir=tmp_path), run=fake_run)
    runner.start()
    job = runner.submit(GRID[1:], True)
    wait_for(lambda: runner.get(job.id).items[0].status == "done")
    assert runner.get(job.id).items[0].key == "key-orb-NQ"
    saved = from_json(json.loads((tmp_path / "jobs" / f"{job.id}.json").read_text()))
    assert saved.items[0].status == "done"


def test_failing_item_does_not_stop_the_queue(tmp_path):
    runner = JobRunner(Settings(data_dir=tmp_path), run=fake_run)
    runner.start()
    job = runner.submit(GRID, True)
    wait_for(lambda: runner.get(job.id).items[1].status == "done")
    bad = runner.get(job.id).items[0]
    assert (bad.status, bad.error) == ("error", "ValueError: boom")


def test_restart_reruns_only_unfinished_items(tmp_path):
    job = Job(
        "j1",
        "2026-10-10T14:00:00Z",
        True,
        (
            JobItem("strategies/a.py", "lucid-flex-50k", "NQ", status="done", key="old"),
            JobItem("strategies/b.py", "lucid-flex-50k", "NQ", status="running", progress=(3, 9)),
        ),
    )
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j1.json").write_text(json.dumps(to_json(job)))
    calls: list[str] = []

    def run(item, settings, **kwargs):
        calls.append(str(item.strategy_path))
        return {"key": "new"}

    runner = JobRunner(Settings(data_dir=tmp_path), run=run)
    runner.start()
    wait_for(lambda: runner.get("j1").items[1].status == "done")
    assert calls == ["strategies/b.py"]
    assert runner.get("j1").items[0].key == "old"


def test_listeners_receive_job_changes(tmp_path):
    runner = JobRunner(Settings(data_dir=tmp_path), run=fake_run)
    loop = asyncio.new_event_loop()
    q = runner.subscribe(loop)
    runner.start()
    job = runner.submit(GRID[1:], True)

    async def until_done() -> list[str]:
        seen = []
        while not seen or seen[-1] != "done":
            seen.append((await q.get())["items"][0]["status"])
        return seen

    seen = loop.run_until_complete(asyncio.wait_for(until_done(), 5))
    assert seen[0] == "pending" and "running" in seen and seen[-1] == "done"
    runner.unsubscribe(q)
    loop.close()
    assert runner.jobs()[0].id == job.id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_jobs_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.server.jobs_runner'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/server/jobs_runner.py`:

```python
"""Shell for backtest jobs: data/jobs/<id>.json, one worker thread, WebSocket listeners."""

import asyncio
import json
import queue
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from fenrir.backtest.cache import cached_run
from fenrir.backtest.grid import GridItem
from fenrir.server.jobs import (
    Job,
    from_json,
    grid_item,
    new_job,
    progress_step,
    restart,
    to_json,
    unfinished,
    update_item,
)
from fenrir.settings import Settings


class JobRunner:
    def __init__(self, settings: Settings, run: Callable[..., Any] = cached_run) -> None:
        self._settings = settings
        self._run = run
        self._dir = settings.data_dir / "jobs"
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[tuple[str, int]] = queue.SimpleQueue()
        self._listeners: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]] = set()
        self._jobs: dict[str, Job] = {}
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        for path in sorted(self._dir.glob("*.json")):
            job = restart(from_json(json.loads(path.read_text())))
            self._set(job, persist=True)
            for i in unfinished(job):
                self._queue.put((job.id, i))
        threading.Thread(target=self._work, name="backtest-jobs", daemon=True).start()

    def submit(self, grid: Sequence[GridItem], include_holdout: bool) -> Job:
        created = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        job = new_job(uuid.uuid4().hex, created, grid, include_holdout)
        self._set(job, persist=True)
        for i in range(len(job.items)):
            self._queue.put((job.id, i))
        return job

    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        with self._lock:
            self._listeners.add((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        with self._lock:
            self._listeners = {(lp, lq) for lp, lq in self._listeners if lq is not q}

    def _set(self, job: Job, *, persist: bool) -> None:
        if persist:  # write first, so a job seen as done in memory is also done on disk
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dir / f"{job.id}.json.tmp"
            tmp.write_text(json.dumps(to_json(job)))
            tmp.replace(self._dir / f"{job.id}.json")
        with self._lock:
            self._jobs[job.id] = job
            listeners = list(self._listeners)
        payload = to_json(job)
        for loop, q in listeners:
            loop.call_soon_threadsafe(q.put_nowait, payload)

    def _update(self, job_id: str, index: int, *, persist: bool, **changes: Any) -> None:
        self._set(update_item(self._jobs[job_id], index, **changes), persist=persist)

    def _work(self) -> None:
        while True:
            job_id, i = self._queue.get()
            job = self._jobs[job_id]
            self._update(job_id, i, persist=True, status="running")

            def on_progress(done: int, total: int, job_id: str = job_id, i: int = i) -> None:
                if progress_step(self._jobs[job_id].items[i].progress, (done, total)):
                    self._update(job_id, i, persist=False, progress=(done, total))

            try:
                report = self._run(
                    grid_item(job.items[i]),
                    self._settings,
                    include_holdout=job.include_holdout,
                    on_progress=on_progress,
                )
                self._update(job_id, i, persist=True, status="done", key=report["key"])
            except Exception as e:  # one bad pair must not stop the rest of the grid
                self._update(job_id, i, persist=True, status="error", error=f"{type(e).__name__}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/server/test_jobs_runner.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/server/jobs_runner.py tests/server/test_jobs_runner.py
git commit -m "feat(server): run backtest jobs on a worker thread and resume them on start

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pure comparison ranking

**Files:**
- Create: `src/fenrir/backtest/compare.py`
- Modify: `src/fenrir/backtest/CLAUDE.md`
- Test: `tests/backtest/test_compare.py`

**Interfaces:**
- Consumes: `Report` (TypedDict, `fenrir.backtest.report`, plan 09).
- Produces:
  - `View = Literal["account", "strategy"]`
  - `summarize(report: Report) -> dict` — the report without `sample_equity` in any period.
  - `latest_only(reports: Sequence[Report]) -> tuple[Report, ...]` — input newest first; keeps the newest per `(strategy, account, contract)`.
  - `compare(reports: Sequence[Report], view: View) -> list[dict]` — `[{"group": str, "rows": [summary, ...]}]`, groups sorted by name, rows sorted by tuning `p_pass_and_payout` desc, then tuning `expected_net_per_attempt` desc, then names.

- [ ] **Step 1: Write the failing tests**

`tests/backtest/test_compare.py`:

```python
from fenrir.backtest.compare import compare, latest_only, summarize


def period(p: float, net: float) -> dict:
    return {
        "start": "2020-01-02", "end": "2024-10-09", "attempts": 10, "pass_rate": 0.5,
        "days_to_pass": {"median": 5, "histogram": [[5, 5]]},
        "failure_reasons": {"max_drawdown": 5},
        "payout_rate": 0.5, "median_days_to_payout": 9, "median_first_payout": 1000.0,
        "p_pass_and_payout": p, "expected_net_per_attempt": net, "microscalp_flags": 0,
        "sample_equity": [{"start": "2020-01-02", "outcome": "payout", "points": []}],
    }


def report(strategy, account, contract="NQ", tuning=(0.2, 50.0), holdout=(0.2, 50.0), created="2026-10-10T00:00:00Z"):
    return {
        "key": f"{strategy}-{account}-{contract}-{created}", "created_at": created,
        "strategy": strategy, "strategy_hash": "h", "params": {},
        "account": account, "rules_hash": "r", "contract": contract, "data_version": "d",
        "holdout_months": 12,
        "periods": {"tuning": period(*tuning), "holdout": None if holdout is None else period(*holdout)},
        "holdout_gap": False, "note": "n",
    }


def test_summarize_drops_sample_equity_everywhere():
    s = summarize(report("orb", "lucid-flex-50k"))
    assert "sample_equity" not in s["periods"]["tuning"]
    assert "sample_equity" not in s["periods"]["holdout"]
    assert summarize(report("orb", "a", holdout=None))["periods"]["holdout"] is None


def test_latest_only_keeps_newest_per_triple():
    newer = report("orb", "a", created="2026-10-10T02:00:00Z")
    older = report("orb", "a", created="2026-10-10T01:00:00Z")
    other = report("orb", "a", contract="MNQ")
    assert latest_only([newer, older, other]) == (newer, other)


def test_by_account_ranks_strategies_by_tuning():
    rows = [
        report("low", "a", tuning=(0.1, 10.0)),
        report("high", "a", tuning=(0.3, 10.0)),
        report("tie_rich", "a", tuning=(0.1, 90.0)),
        report("x", "b"),
    ]
    out = compare(rows, "account")
    assert [g["group"] for g in out] == ["a", "b"]
    assert [r["strategy"] for r in out[0]["rows"]] == ["high", "tie_rich", "low"]


def test_by_strategy_groups_by_strategy():
    rows = [report("orb", "lucid-pro-50k", tuning=(0.1, 0.0)), report("orb", "lucid-flex-50k", tuning=(0.4, 0.0))]
    out = compare(rows, "strategy")
    assert [r["account"] for r in out[0]["rows"]] == ["lucid-flex-50k", "lucid-pro-50k"]


def test_holdout_never_changes_order():
    a = report("a", "acct", tuning=(0.3, 10.0), holdout=(0.0, -500.0))
    b = report("b", "acct", tuning=(0.2, 10.0), holdout=(0.9, 900.0))
    assert [r["strategy"] for r in compare([b, a], "account")[0]["rows"]] == ["a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/backtest/test_compare.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest.compare'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/backtest/compare.py`:

```python
"""Pure ranking for the comparison table. Sorts by tuning results only; hold-out is shown, never sorted on."""

from collections.abc import Sequence
from typing import Literal

from fenrir.backtest.report import Report

View = Literal["account", "strategy"]


def summarize(report: Report) -> dict:
    periods = {
        name: None if p is None else {k: v for k, v in p.items() if k != "sample_equity"}
        for name, p in report["periods"].items()
    }
    return {**report, "periods": periods}


def latest_only(reports: Sequence[Report]) -> tuple[Report, ...]:
    # Input is newest first; iterating oldest first lets the newest overwrite.
    return tuple({(r["strategy"], r["account"], r["contract"]): r for r in reversed(reports)}.values())[::-1]


def _rank(r: Report) -> tuple:
    t = r["periods"]["tuning"]
    return (-t["p_pass_and_payout"], -t["expected_net_per_attempt"], r["strategy"], r["account"], r["contract"])


def compare(reports: Sequence[Report], view: View) -> list[dict]:
    field = "account" if view == "account" else "strategy"
    return [
        {"group": g, "rows": [summarize(r) for r in sorted((r for r in reports if r[field] == g), key=_rank)]}
        for g in sorted({r[field] for r in reports})
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/backtest/test_compare.py -q`
Expected: `5 passed`

- [ ] **Step 5: Update the directory context file**

Append to the bullet list in `src/fenrir/backtest/CLAUDE.md`:

```markdown
- Comparison: `compare.py` (`compare`, `latest_only`, `summarize`) ranks reports by tuning results only; used by the server's comparison table.
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/backtest/compare.py src/fenrir/backtest/CLAUDE.md tests/backtest/test_compare.py
git commit -m "feat(backtest): rank reports by tuning results for the comparison table

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Backtests API and WebSocket

**Files:**
- Create: `src/fenrir/server/routes/backtests.py`
- Modify: `src/fenrir/server/app.py` (register routers, create the runner)
- Modify: `src/fenrir/cli/serve.py` (start the runner)
- Test: `tests/server/test_backtests_routes.py`

**Interfaces:**
- Consumes: `JobRunner` (Task 2), `compare`/`latest_only`/`summarize` (Task 3), `expand_grid`, `list_reports` (plan 10), `list_accounts` (plan 05), `require_token`, `check_ws` (plan 11), `app.state.settings` (plan 11).
- Produces (all under `X-Fenrir-Token`):
  - `GET /api/backtests/options` → `{strategies: string[], accounts: string[], contracts: ["ES","MES","NQ","MNQ"]}` (strategies are `strategies_dir/*.py` paths as strings, skipping `_*.py`).
  - `POST /api/backtests/jobs` `{strategies, accounts, contracts: string[] | null, include_holdout: boolean = true}` → `{job_id}`; 422 if a strategy is not in `options.strategies` or nothing matches.
  - `GET /api/backtests/jobs` → `Job[]` newest first; `GET /api/backtests/jobs/{id}` → `Job` | 404. `Job` JSON = `to_json(job)` (contract shape plus `include_holdout`).
  - `GET /api/backtests/results` → summaries (no `sample_equity`), newest first; `GET /api/backtests/results/{key}` → full `Report` | 404.
  - `GET /api/backtests/compare?view=account|strategy` → `[{group, rows: summary[]}]`.
  - `WS /ws/jobs?token=` → `Job` JSON on every change.
  - `app.state.jobs: JobRunner`.

- [ ] **Step 1: Write the failing tests**

`tests/server/test_backtests_routes.py`:

```python
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fenrir.server.app import create_app
from fenrir.server.jobs_runner import JobRunner
from fenrir.server.routes import backtests as routes
from fenrir.settings import Settings

TOKEN = "t"
AUTH = {"X-Fenrir-Token": TOKEN}
STRATEGY = 'MARKET = "NQ"\nPARAMS = {}\n\ndef init(params):\n    return None\n\ndef step(state, bar, ctx):\n    return state, []\n'
calls: list[str] = []


def fake_run(item, settings, *, include_holdout, on_progress=None):
    calls.append(str(item.strategy_path))
    return {"key": f"key-{item.contract}"}


@pytest.fixture
def env(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "orb.py").write_text(STRATEGY)
    (tmp_path / "strategies" / "_helpers.py").write_text("")
    rules = tmp_path / "rules" / "lucid"
    rules.mkdir(parents=True)
    src = sorted(Path("rules/lucid").glob("*.yaml"))[0]  # any real, valid account file from plan 05
    shutil.copy(src, rules / src.name)
    settings = Settings(data_dir=tmp_path / "data", rules_dir=tmp_path / "rules", strategies_dir=tmp_path / "strategies")
    app = create_app(settings, TOKEN, None)
    app.state.jobs = JobRunner(settings, run=fake_run)
    app.state.jobs.start()
    calls.clear()
    return TestClient(app, base_url="http://127.0.0.1:8000"), settings


def test_options_lists_strategies_accounts_contracts(env):
    client, settings = env
    body = client.get("/api/backtests/options", headers=AUTH).json()
    assert body["strategies"] == [str(settings.strategies_dir / "orb.py")]
    assert len(body["accounts"]) == 1 and body["accounts"][0].startswith("lucid-")
    assert body["contracts"] == ["ES", "MES", "NQ", "MNQ"]


def test_create_job_runs_and_reports_done(env):
    client, settings = env
    options = client.get("/api/backtests/options", headers=AUTH).json()
    r = client.post(
        "/api/backtests/jobs",
        headers=AUTH,
        json={"strategies": options["strategies"], "accounts": options["accounts"], "contracts": ["NQ"]},
    )
    job_id = r.json()["job_id"]
    for _ in range(500):
        job = client.get(f"/api/backtests/jobs/{job_id}", headers=AUTH).json()
        if job["items"][0]["status"] == "done":
            break
        time.sleep(0.01)
    assert job["items"][0]["key"] == "key-NQ"
    assert job["include_holdout"] is True
    assert client.get("/api/backtests/jobs", headers=AUTH).json()[0]["id"] == job_id


def test_create_job_rejects_unknown_strategy_path(env):
    client, _ = env
    r = client.post(
        "/api/backtests/jobs",
        headers=AUTH,
        json={"strategies": ["../../tmp/evil.py"], "accounts": ["lucid-*"], "contracts": None},
    )
    assert r.status_code == 422
    assert calls == []


def test_create_job_rejects_empty_match(env):
    client, settings = env
    r = client.post(
        "/api/backtests/jobs",
        headers=AUTH,
        json={"strategies": [str(settings.strategies_dir / "orb.py")], "accounts": ["nope-*"], "contracts": None},
    )
    assert r.status_code == 422


def test_unknown_job_is_404(env):
    client, _ = env
    assert client.get("/api/backtests/jobs/nope", headers=AUTH).status_code == 404


def _report(key: str, strategy: str, p: float) -> dict:
    period = {"p_pass_and_payout": p, "expected_net_per_attempt": 0.0, "sample_equity": [{"points": []}]}
    return {"key": key, "strategy": strategy, "account": "a", "contract": "NQ",
            "periods": {"tuning": period, "holdout": None}}


def test_results_compare_and_single_report(env, monkeypatch):
    client, _ = env
    reports = (_report("k2", "b", 0.1), _report("k1", "a", 0.5))
    monkeypatch.setattr(routes, "list_reports", lambda settings: reports)
    listed = client.get("/api/backtests/results", headers=AUTH).json()
    assert [r["key"] for r in listed] == ["k2", "k1"]
    assert "sample_equity" not in listed[0]["periods"]["tuning"]
    full = client.get("/api/backtests/results/k1", headers=AUTH).json()
    assert full["periods"]["tuning"]["sample_equity"] == [{"points": []}]
    assert client.get("/api/backtests/results/zzz", headers=AUTH).status_code == 404
    groups = client.get("/api/backtests/compare?view=account", headers=AUTH).json()
    assert [r["strategy"] for r in groups[0]["rows"]] == ["a", "b"]


def test_api_needs_token(env):
    client, _ = env
    assert client.get("/api/backtests/jobs").status_code == 401


def test_jobs_socket_needs_token(env):
    client, _ = env
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/jobs", headers={"origin": "http://127.0.0.1:8000"}):
            pass
    assert e.value.code == 4401


def test_jobs_socket_pushes_changes(env):
    client, settings = env
    with client.websocket_connect(f"/ws/jobs?token={TOKEN}", headers={"origin": "http://127.0.0.1:8000"}) as ws:
        client.post(
            "/api/backtests/jobs",
            headers=AUTH,
            json={"strategies": [str(settings.strategies_dir / "orb.py")], "accounts": ["lucid-*"], "contracts": ["NQ"]},
        )
        statuses = []
        while not statuses or statuses[-1] != "done":
            statuses.append(ws.receive_json()["items"][0]["status"])
    assert statuses[-1] == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/server/test_backtests_routes.py -q`
Expected: FAIL with `ImportError: cannot import name 'backtests' from 'fenrir.server.routes'`

- [ ] **Step 3: Write the routes**

`src/fenrir/server/routes/backtests.py`:

```python
"""Backtests API: grid jobs, results, comparison. Handlers only parse and forward."""

import asyncio

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from fenrir.backtest.cache import list_reports
from fenrir.backtest.compare import View, compare, latest_only, summarize
from fenrir.backtest.grid import expand_grid
from fenrir.rules.loader import list_accounts
from fenrir.server.auth import check_ws
from fenrir.server.jobs import to_json
from fenrir.settings import Settings

CONTRACTS = ["ES", "MES", "NQ", "MNQ"]

router = APIRouter(prefix="/api/backtests")
ws_router = APIRouter()


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[str] = Field(min_length=1)
    accounts: list[str] = Field(min_length=1)
    contracts: list[str] | None = None
    include_holdout: bool = True


def strategy_files(settings: Settings) -> list[str]:
    return [str(p) for p in sorted(settings.strategies_dir.glob("*.py")) if not p.name.startswith("_")]


@router.get("/options")
def options(request: Request) -> dict:
    s: Settings = request.app.state.settings
    return {"strategies": strategy_files(s), "accounts": list(list_accounts(s.rules_dir)), "contracts": CONTRACTS}


@router.post("/jobs")
def create_job(body: JobRequest, request: Request) -> dict:
    s: Settings = request.app.state.settings
    unknown = set(body.strategies) - set(strategy_files(s))
    if unknown:  # loading a strategy executes it, so only files in strategies/ may run
        raise HTTPException(422, f"Unknown strategy files: {sorted(unknown)}")
    try:
        grid = expand_grid(body.strategies, body.accounts, body.contracts or None, s)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not grid:
        raise HTTPException(422, "No strategy × account × contract combination matched.")
    return {"job_id": request.app.state.jobs.submit(grid, body.include_holdout).id}


@router.get("/jobs")
def list_jobs(request: Request) -> list[dict]:
    return [to_json(j) for j in request.app.state.jobs.jobs()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}")
    return to_json(job)


@router.get("/results")
def results(request: Request) -> list[dict]:
    return [summarize(r) for r in list_reports(request.app.state.settings)]


@router.get("/results/{key}")
def result(key: str, request: Request) -> dict:
    # shortcut: scans every cached report per call; fine for hundreds, index by key if it gets slow
    found = next((r for r in list_reports(request.app.state.settings) if r["key"] == key), None)
    if found is None:
        raise HTTPException(404, f"No report {key}")
    return found


@router.get("/compare")
def comparison(request: Request, view: View = "account") -> list[dict]:
    return compare(latest_only(list_reports(request.app.state.settings)), view)


@ws_router.websocket("/ws/jobs")
async def jobs_socket(ws: WebSocket) -> None:
    if not check_ws(ws):
        await ws.close(code=4401)
        return
    runner = ws.app.state.jobs
    q = runner.subscribe(asyncio.get_running_loop())  # before accept: no change is missed once the client is connected
    try:
        await ws.accept()
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        runner.unsubscribe(q)
```

- [ ] **Step 4: Register the routers and create the runner**

In `src/fenrir/server/app.py`, add the imports next to the other imports:

```python
from fenrir.server.jobs_runner import JobRunner
from fenrir.server.routes import backtests
```

and, under the `# Register routers here, one line each:` comment:

```python
    app.state.jobs = JobRunner(settings)
    app.include_router(backtests.router, dependencies=[Depends(require_token)])
    app.include_router(backtests.ws_router)
```

(`Depends` and `require_token` are already imported there by plan 11; if not, add `from fastapi import Depends` and `from fenrir.server.auth import require_token`.) The runner does no I/O until `start()`, so tests that build the app never touch `data/jobs/`.

- [ ] **Step 5: Start the runner from `fenrir serve`**

In `src/fenrir/cli/serve.py`, directly after the `app = create_app(...)` line:

```python
    app.state.jobs.start()  # resumes items left unfinished by the last run
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/server -q`
Expected: all pass, including `9 passed` from `test_backtests_routes.py` (`uv run pytest tests/server/test_backtests_routes.py -q` → `9 passed`).

- [ ] **Step 7: Commit**

```bash
git add src/fenrir/server/routes/backtests.py src/fenrir/server/app.py src/fenrir/cli/serve.py tests/server/test_backtests_routes.py
git commit -m "feat(server): add backtest jobs, results and comparison API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Backtests page — run form, jobs, comparison table

**Files:**
- Create: `web/src/errorText.ts` (skip if plan 15 or 16 already created it with identical content)
- Create: `web/src/backtests/types.ts`, `web/src/backtests/format.ts`, `web/src/backtests/useJobsSocket.ts`
- Create: `web/src/backtests/RunForm.tsx`, `web/src/backtests/JobList.tsx`, `web/src/backtests/CompareTable.tsx`
- Create: `web/src/backtests/CLAUDE.md`
- Create (by shadcn CLI): `web/src/components/ui/{table,checkbox,label,tabs,badge}.tsx`
- Modify: `web/src/routes/backtests.tsx`
- Modify: `CLAUDE.md` (Directory Map table)

**Interfaces:**
- Consumes: `apiFetch`, `wsUrl`, `ApiError` (`web/src/api.ts`, plan 12); Task 4 API.
- Produces: TS types `PeriodResult`, `Report`, `Job`, `JobItem`, `CompareGroup`, `Options`; `pct(x)`, `money(x)`, `num(x)` in `format.ts`; `errorText(e: unknown): string` in `web/src/errorText.ts`; `useJobsSocket()`; components `RunForm`, `JobList`, `CompareTable`. Query keys: `['backtests','options']`, `['backtests','jobs']`, `['backtests','results']`, `['backtests','compare',view]`, `['backtests','result',key]`.

- [ ] **Step 1: Add the shadcn components this page needs**

Run: `cd web && npx shadcn@latest add table checkbox label tabs badge && cd ..`
Expected: `table.tsx`, `checkbox.tsx`, `label.tsx`, `tabs.tsx`, `badge.tsx` created in `web/src/components/ui/`. (`button` and `alert` exist from plan 12.)

- [ ] **Step 1b: Write the types and formatters**

`web/src/backtests/types.ts`:

```ts
export type PeriodResult = {
  start: string
  end: string
  attempts: number
  pass_rate: number
  days_to_pass: { median: number | null; histogram: [number, number][] }
  failure_reasons: Record<string, number>
  payout_rate: number
  median_days_to_payout: number | null
  median_first_payout: number | null
  p_pass_and_payout: number
  expected_net_per_attempt: number
  microscalp_flags: number
  sample_equity?: { start: string; outcome: string; points: [string, number][] }[]
}

export type Report = {
  key: string
  created_at: string
  strategy: string
  strategy_hash: string
  params: Record<string, unknown>
  account: string
  rules_hash: string
  contract: string
  data_version: string
  holdout_months: number
  periods: { tuning: PeriodResult; holdout: PeriodResult | null }
  holdout_gap: boolean
  note: string
}

export type JobItem = {
  strategy: string
  account: string
  contract: string
  status: 'pending' | 'running' | 'done' | 'error'
  key: string | null
  error: string | null
  progress: [number, number]
}

export type Job = { id: string; created_at: string; include_holdout: boolean; items: JobItem[] }

export type CompareGroup = { group: string; rows: Report[] }

export type Options = { strategies: string[]; accounts: string[]; contracts: string[] }
```

`web/src/backtests/format.ts`:

```ts
export const pct = (x: number | null | undefined): string => (x == null ? '—' : `${(x * 100).toFixed(1)}%`)
export const money = (x: number | null | undefined): string =>
  x == null ? '—' : x.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
export const num = (x: number | null | undefined): string => (x == null ? '—' : String(x))
```

`web/src/errorText.ts` (shared by plans 14, 15 and 16; identical in each):

```ts
import { ApiError } from './api'

/** FastAPI errors arrive as `{"detail": ...}`; show the detail, not the raw JSON. */
export function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return String(e)
  try {
    const detail = (JSON.parse(e.body) as { detail?: unknown }).detail
    return typeof detail === 'string' ? detail : JSON.stringify(detail)
  } catch {
    return e.message
  }
}
```

- [ ] **Step 2: Write the job socket hook**

`web/src/backtests/useJobsSocket.ts`:

```ts
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { wsUrl } from '../api'
import type { Job } from './types'

/** Keeps ['backtests','jobs'] live and refreshes results when an item finishes. */
export function useJobsSocket(): void {
  const qc = useQueryClient()
  const finished = useRef(new Map<string, number>())
  useEffect(() => {
    const ws = new WebSocket(wsUrl('/ws/jobs'))
    ws.onmessage = (e: MessageEvent<string>) => {
      const job = JSON.parse(e.data) as Job
      qc.setQueryData<Job[]>(['backtests', 'jobs'], (jobs = []) =>
        [job, ...jobs.filter((j) => j.id !== job.id)].sort((a, b) => b.created_at.localeCompare(a.created_at)),
      )
      const n = job.items.filter((i) => i.status === 'done' || i.status === 'error').length
      if (n > (finished.current.get(job.id) ?? 0)) {
        void qc.invalidateQueries({ queryKey: ['backtests', 'results'] })
        void qc.invalidateQueries({ queryKey: ['backtests', 'compare'] })
      }
      finished.current.set(job.id, n)
    }
    return () => ws.close()
  }, [qc])
}
```

- [ ] **Step 3: Write the run form**

`web/src/backtests/RunForm.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { apiFetch } from '../api'
import { errorText } from '../errorText'
import type { Options } from './types'

function Picks({ legend, values, picked, toggle }: { legend: string; values: string[]; picked: Set<string>; toggle: (v: string) => void }) {
  const base = useId()
  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm font-medium text-muted-foreground">{legend}</legend>
      {values.map((v, i) => (
        <div key={v} className="flex items-center gap-2">
          <Checkbox id={`${base}-${i}`} checked={picked.has(v)} onCheckedChange={() => toggle(v)} />
          <Label htmlFor={`${base}-${i}`}>{v}</Label>
        </div>
      ))}
    </fieldset>
  )
}

function usePicks(): [Set<string>, (v: string) => void] {
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const toggle = (v: string) =>
    setPicked((old) => {
      const next = new Set(old)
      if (next.has(v)) next.delete(v)
      else next.add(v)
      return next
    })
  return [picked, toggle]
}

export function RunForm() {
  const qc = useQueryClient()
  const options = useQuery({ queryKey: ['backtests', 'options'], queryFn: () => apiFetch<Options>('/api/backtests/options') })
  const [strategies, toggleStrategy] = usePicks()
  const [accounts, toggleAccount] = usePicks()
  const [contracts, toggleContract] = usePicks()
  const run = useMutation({
    mutationFn: () =>
      apiFetch<{ job_id: string }>('/api/backtests/jobs', {
        method: 'POST',
        body: JSON.stringify({
          strategies: [...strategies],
          accounts: [...accounts],
          contracts: contracts.size ? [...contracts] : null,
          include_holdout: true,
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['backtests', 'jobs'] }),
  })
  if (!options.data)
    return <p className="text-muted-foreground">{options.error ? errorText(options.error) : 'Loading…'}</p>
  return (
    <form
      className="mb-6 space-y-4"
      onSubmit={(e) => {
        e.preventDefault()
        run.mutate()
      }}
    >
      <div className="grid gap-6 md:grid-cols-3">
        <Picks legend="Strategies" values={options.data.strategies} picked={strategies} toggle={toggleStrategy} />
        <Picks legend="Accounts" values={options.data.accounts} picked={accounts} toggle={toggleAccount} />
        <Picks legend="Contracts (none = both for the strategy's market)" values={options.data.contracts} picked={contracts} toggle={toggleContract} />
      </div>
      <Button type="submit" disabled={!strategies.size || !accounts.size || run.isPending}>
        Run
      </Button>
      {run.error && (
        <Alert variant="destructive">
          <AlertDescription>{errorText(run.error)}</AlertDescription>
        </Alert>
      )}
    </form>
  )
}
```

- [ ] **Step 4: Write the job list**

`web/src/backtests/JobList.tsx`:

```tsx
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { apiFetch } from '../api'
import type { Job, JobItem } from './types'

const VARIANT = { pending: 'outline', running: 'secondary', done: 'default', error: 'destructive' } as const satisfies Record<
  JobItem['status'],
  'outline' | 'secondary' | 'default' | 'destructive'
>

export function JobList() {
  // No polling: useJobsSocket keeps this query current.
  const jobs = useQuery({ queryKey: ['backtests', 'jobs'], queryFn: () => apiFetch<Job[]>('/api/backtests/jobs') })
  const recent = (jobs.data ?? []).slice(0, 5)
  if (!recent.length) return null
  return (
    <Table className="mb-6">
      <TableCaption className="caption-top text-left">Recent jobs</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Strategy</TableHead>
          <TableHead>Account</TableHead>
          <TableHead>Contract</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Progress</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {recent.flatMap((job) =>
          job.items.map((it, i) => (
            <TableRow key={`${job.id}-${i}`}>
              <TableCell>{it.strategy}</TableCell>
              <TableCell>{it.account}</TableCell>
              <TableCell>{it.contract}</TableCell>
              <TableCell title={it.error ?? undefined}>
                <Badge variant={VARIANT[it.status]} asChild={!!it.key}>
                  {it.key ? <Link to="/backtests/$key" params={{ key: it.key }}>{it.status}</Link> : <span>{it.status}</span>}
                </Badge>
              </TableCell>
              <TableCell>{it.progress[1] ? `${Math.floor((it.progress[0] * 100) / it.progress[1])}%` : ''}</TableCell>
            </TableRow>
          )),
        )}
      </TableBody>
    </Table>
  )
}
```

- [ ] **Step 5: Write the comparison table**

`web/src/backtests/CompareTable.tsx`:

```tsx
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { apiFetch } from '../api'
import { money, pct } from './format'
import type { CompareGroup } from './types'

type View = 'account' | 'strategy'

export function CompareTable() {
  const [view, setView] = useState<View>('account')
  const groups = useQuery({
    queryKey: ['backtests', 'compare', view],
    queryFn: () => apiFetch<CompareGroup[]>(`/api/backtests/compare?view=${view}`),
  })
  return (
    <section>
      <h2>Comparison</h2>
      <Tabs value={view} onValueChange={(v) => setView(v as View)}>
        <TabsList aria-label="View">
          <TabsTrigger value="account">By account</TabsTrigger>
          <TabsTrigger value="strategy">By strategy</TabsTrigger>
        </TabsList>
        <TabsContent value={view} className="space-y-6">
          <p className="text-sm text-muted-foreground">
            Ranked by tuning-period results. Hold-out columns are shown for checking, never for sorting.
          </p>
          {(groups.data ?? []).map((g) => (
            <Table key={g.group}>
              <TableCaption className="caption-top text-left">{g.group}</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>{view === 'account' ? 'Strategy' : 'Account'}</TableHead>
                  <TableHead>Contract</TableHead>
                  <TableHead>Tuning P(pass &amp; payout)</TableHead>
                  <TableHead>Tuning net / attempt</TableHead>
                  <TableHead>Tuning pass rate</TableHead>
                  <TableHead>Hold-out P(pass &amp; payout)</TableHead>
                  <TableHead>Hold-out net / attempt</TableHead>
                  <TableHead>Flags</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {g.rows.map((r) => (
                  <TableRow key={r.key}>
                    <TableCell>
                      <Link className="text-primary underline-offset-4 hover:underline" to="/backtests/$key" params={{ key: r.key }}>
                        {view === 'account' ? r.strategy : r.account}
                      </Link>
                    </TableCell>
                    <TableCell>{r.contract}</TableCell>
                    <TableCell>{pct(r.periods.tuning.p_pass_and_payout)}</TableCell>
                    <TableCell>{money(r.periods.tuning.expected_net_per_attempt)}</TableCell>
                    <TableCell>{pct(r.periods.tuning.pass_rate)}</TableCell>
                    <TableCell className="text-muted-foreground">{pct(r.periods.holdout?.p_pass_and_payout)}</TableCell>
                    <TableCell className="text-muted-foreground">{money(r.periods.holdout?.expected_net_per_attempt)}</TableCell>
                    <TableCell className="space-x-1">
                      {r.holdout_gap && <Badge variant="destructive">hold-out gap</Badge>}
                      {r.periods.tuning.microscalp_flags > 0 && <Badge variant="outline">microscalp</Badge>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ))}
        </TabsContent>
      </Tabs>
    </section>
  )
}
```

- [ ] **Step 6: Wire the page**

In `web/src/routes/backtests.tsx`, keep the imports and `createRoute({...})` call from plan 12, change `component: () => <h1>Backtests</h1>` to `component: BacktestsPage`, and add:

```tsx
import { CompareTable } from '../backtests/CompareTable'
import { JobList } from '../backtests/JobList'
import { RunForm } from '../backtests/RunForm'
import { useJobsSocket } from '../backtests/useJobsSocket'

function BacktestsPage() {
  useJobsSocket()
  return (
    <>
      <h1>Backtests</h1>
      <RunForm />
      <JobList />
      <CompareTable />
    </>
  )
}
```

- [ ] **Step 7: Add the directory context file and map row**

`web/src/backtests/CLAUDE.md`:

```markdown
# web/src/backtests/

Backtest UI pieces: run form, live job list, comparison table, report view, equity charts, Workbench latest-report pane.

- Entry points: `types.ts` (Report/Job JSON types, mirror `fenrir.backtest.report` and `fenrir.server.jobs`), `useJobsSocket.ts` (`/ws/jobs` → query cache), `CompareTable.tsx`, `ReportView.tsx`, `LatestReport.tsx`.
- Pure vs shell: `format.ts` is pure; components read server state only through TanStack Query (`['backtests', ...]` keys).
- UI: shadcn components (`@/components/ui/*`) + Tailwind classes; charts are `lightweight-charts` colored from the `--chart-*` CSS variables.
- Not here: ranking logic (server-side, `src/fenrir/backtest/compare.py`); page routes (`web/src/routes/backtests.tsx`, `backtest-report.tsx`).
- Tests: Playwright `web/e2e/backtest.spec.ts`.
```

In the root `CLAUDE.md` Directory Map table, add after the `web/` row:

```markdown
| `web/src/backtests/` | Backtest UI components: run form, live jobs, comparison table, report view, equity charts. |
```

- [ ] **Step 8: Typecheck**

Run: `npm --prefix web run typecheck`
Expected: exits 0 with no errors.

- [ ] **Step 9: Commit**

```bash
git add web/src/errorText.ts web/src/backtests web/src/components/ui web/package.json web/package-lock.json web/src/routes/backtests.tsx CLAUDE.md
git commit -m "feat(web): add backtest run form, live jobs and comparison table

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Report page with histogram, failure reasons and equity curves

**Files:**
- Create: `web/src/backtests/EquityChart.tsx`, `web/src/backtests/ReportView.tsx`
- Modify: `web/src/routes/backtest-report.tsx`
- Modify: `web/package.json`, `web/package-lock.json` (add `lightweight-charts`)

**Interfaces:**
- Consumes: `GET /api/backtests/results/{key}` (Task 4), types and formatters (Task 5).
- Produces: `PeriodTable({ periods })` (tuning and hold-out side by side; used by Task 7), `ReportView({ report })`, `EquityChart({ title, points })`.

- [ ] **Step 1: Install Lightweight Charts**

Run: `npm --prefix web install lightweight-charts@^5`
Expected: `added 2 packages` (or similar), `web/package.json` lists `"lightweight-charts": "^5..."` under `dependencies`.

- [ ] **Step 2: Write the equity chart**

`web/src/backtests/EquityChart.tsx`:

```tsx
import { createChart, LineSeries, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef } from 'react'

// The chart draws on a canvas, so it reads theme tokens instead of Tailwind classes.
const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

export function EquityChart({ title, points }: { title: string; points: [string, number][] }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!box.current) return
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: cssVar('--card') }, textColor: cssVar('--muted-foreground') },
      grid: { vertLines: { color: cssVar('--border') }, horzLines: { color: cssVar('--border') } },
    })
    chart
      .addSeries(LineSeries, { color: cssVar('--chart-1'), lineWidth: 2 })
      .setData(points.map(([ts, value]) => ({ time: (Date.parse(ts) / 1000) as UTCTimestamp, value })))
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [points])
  return (
    <figure className="mb-4">
      <figcaption className="mb-1 text-sm text-muted-foreground">{title}</figcaption>
      <div ref={box} className="h-44 rounded-md border" role="img" aria-label={`Equity curve: ${title}`} />
    </figure>
  )
}
```

- [ ] **Step 3: Write the report view**

`web/src/backtests/ReportView.tsx`:

```tsx
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EquityChart } from './EquityChart'
import { money, num, pct } from './format'
import type { PeriodResult, Report } from './types'

const ROWS: [string, (p: PeriodResult) => string][] = [
  ['Period', (p) => `${p.start} → ${p.end}`],
  ['Attempts', (p) => num(p.attempts)],
  ['Pass rate', (p) => pct(p.pass_rate)],
  ['Median days to pass', (p) => num(p.days_to_pass.median)],
  ['Payout rate (of passed)', (p) => pct(p.payout_rate)],
  ['Median days to first payout', (p) => num(p.median_days_to_payout)],
  ['Median first payout', (p) => money(p.median_first_payout)],
  ['P(pass and first payout)', (p) => pct(p.p_pass_and_payout)],
  ['Expected net $ per attempt', (p) => money(p.expected_net_per_attempt)],
  ['Microscalp flags', (p) => num(p.microscalp_flags)],
]

export function PeriodTable({ periods }: { periods: Report['periods'] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Metric</TableHead>
          <TableHead scope="col">Tuning</TableHead>
          <TableHead scope="col">Hold-out</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ROWS.map(([label, cell]) => (
          <TableRow key={label}>
            <TableHead scope="row">{label}</TableHead>
            <TableCell>{cell(periods.tuning)}</TableCell>
            <TableCell className="text-muted-foreground">{periods.holdout ? cell(periods.holdout) : '—'}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Histogram({ data }: { data: [number, number][] }) {
  const max = Math.max(1, ...data.map(([, n]) => n))
  return (
    <Table aria-label="Days to pass">
      <TableBody>
        {data.map(([days, n]) => (
          <TableRow key={days}>
            <TableHead scope="row" className="w-12">{days}d</TableHead>
            <TableCell className="w-full">
              {/* width is data, so it stays an inline style */}
              <div className="h-3 rounded-sm bg-chart-1" style={{ width: `${(n * 100) / max}%` }} />
            </TableCell>
            <TableCell className="text-right">{n}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function PeriodDetail({ name, period }: { name: string; period: PeriodResult }) {
  return (
    <section className="mt-8 space-y-3">
      <h2>{name}</h2>
      <h3 className="font-medium">Days to pass</h3>
      {period.days_to_pass.histogram.length ? (
        <Histogram data={period.days_to_pass.histogram} />
      ) : (
        <p className="text-muted-foreground">No passes.</p>
      )}
      <h3 className="font-medium">Failure reasons</h3>
      <Table>
        <TableBody>
          {Object.entries(period.failure_reasons).map(([reason, n]) => (
            <TableRow key={reason}>
              <TableHead scope="row">{reason}</TableHead>
              <TableCell className="text-right">{n}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <h3 className="font-medium">Sample equity curves</h3>
      {(period.sample_equity ?? []).map((s) => (
        <EquityChart key={`${s.start}-${s.outcome}`} title={`${s.start} · ${s.outcome}`} points={s.points} />
      ))}
    </section>
  )
}

export function ReportView({ report }: { report: Report }) {
  return (
    <>
      <h1>
        {report.strategy} · {report.account} · {report.contract}
      </h1>
      {report.holdout_gap && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>Hold-out P(pass and payout) is far below tuning: likely overfit.</AlertDescription>
        </Alert>
      )}
      <PeriodTable periods={report.periods} />
      <p className="mt-2 text-sm text-muted-foreground">{report.note}</p>
      <PeriodDetail name="Tuning" period={report.periods.tuning} />
      {report.periods.holdout && <PeriodDetail name="Hold-out" period={report.periods.holdout} />}
    </>
  )
}
```

- [ ] **Step 4: Wire the route**

Replace the `BacktestReport` function in `web/src/routes/backtest-report.tsx` (keep the plan 12 imports and `createRoute` call) and add the imports:

```tsx
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../api'
import { errorText } from '../errorText'
import { ReportView } from '../backtests/ReportView'
import type { Report } from '../backtests/types'

function BacktestReport() {
  const { key } = backtestReportRoute.useParams()
  const report = useQuery({
    queryKey: ['backtests', 'result', key],
    queryFn: () => apiFetch<Report>(`/api/backtests/results/${encodeURIComponent(key)}`),
  })
  if (report.error) return <p className="text-destructive">{errorText(report.error)}</p>
  if (!report.data) return <p className="text-muted-foreground">Loading…</p>
  return <ReportView report={report.data} />
}
```

- [ ] **Step 5: Typecheck**

Run: `npm --prefix web run typecheck`
Expected: exits 0 with no errors.

- [ ] **Step 6: Commit**

```bash
git add web/package.json web/package-lock.json web/src/backtests web/src/routes/backtest-report.tsx
git commit -m "feat(web): add backtest report page with histogram and equity curves

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Workbench latest-report pane

**Files:**
- Create: `web/src/backtests/LatestReport.tsx`
- Modify: `web/src/routes/workbench.tsx`

**Interfaces:**
- Consumes: `GET /api/backtests/results` (Task 4), `PeriodTable` (Task 6), the Workbench layout from plan 13 (terminal in the left column).
- Produces: `LatestReport()` component.

- [ ] **Step 1: Write the pane**

`web/src/backtests/LatestReport.tsx`:

```tsx
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { apiFetch } from '../api'
import { PeriodTable } from './ReportView'
import type { Report } from './types'

export function LatestReport() {
  // Polls because `fenrir backtest` runs in the terminal write reports without going through /ws/jobs.
  const results = useQuery({
    queryKey: ['backtests', 'results'],
    queryFn: () => apiFetch<Report[]>('/api/backtests/results'),
    refetchInterval: 5000,
  })
  const latest = results.data?.[0]
  if (!latest) return <p className="text-muted-foreground">No backtests yet. Ask Claude to run `fenrir backtest`.</p>
  return (
    <div>
      <h2>
        <Link className="text-primary underline-offset-4 hover:underline" to="/backtests/$key" params={{ key: latest.key }}>
          {latest.strategy} · {latest.account} · {latest.contract}
        </Link>
      </h2>
      <p className="mb-2 text-sm text-muted-foreground">{new Date(latest.created_at).toLocaleString()}</p>
      <PeriodTable periods={latest.periods} />
    </div>
  )
}
```

- [ ] **Step 2: Put it in the Workbench's right column**

Replace `web/src/routes/workbench.tsx` (plan 13's version) with; only the `LatestReport` import and the right card's content change:

```tsx
import { createRoute } from '@tanstack/react-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { LatestReport } from '../backtests/LatestReport'
import { Terminal } from '../terminal'
import { rootRoute } from './root'

export const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: Workbench,
})

function Workbench() {
  return (
    <>
      <h1>Workbench</h1>
      <div className="grid h-[calc(100vh-9rem)] grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-5">
        <Card className="min-h-0 p-1">
          <Terminal />
        </Card>
        <Card role="region" aria-label="Latest backtest" className="min-h-0 overflow-auto">
          <CardHeader>
            <CardTitle>Latest backtest</CardTitle>
          </CardHeader>
          <CardContent>
            <LatestReport />
          </CardContent>
        </Card>
      </div>
    </>
  )
}
```

- [ ] **Step 3: Typecheck**

Run: `npm --prefix web run typecheck`
Expected: exits 0 with no errors.

- [ ] **Step 4: Commit**

```bash
git add web/src/backtests/LatestReport.tsx web/src/routes/workbench.tsx
git commit -m "feat(web): show the latest backtest report on the workbench

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Playwright backtest flow on fixture data

**Files:**
- Create: `tests/fixtures/e2e/make_bars.py`
- Create: `tests/fixtures/e2e/bars/` (generated, committed)
- Create: `web/e2e/backtest.spec.ts`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `write_bars(data_dir, series, contract, bars)` from `fenrir.data.store` (plan 03), `front_month`, `load_instruments` (plan 01), `strategies/orb.py` (plan 07), any `rules/lucid/*.yaml` (plan 05), the Playwright config from plan 12 (`FENRIR_DATA_DIR=tests/fixtures/e2e`, server run from the repo root).
- Produces: a committed 10-day synthetic NQ data set in `tests/fixtures/e2e/bars/` reusable by later e2e flows.

- [ ] **Step 1: Write the fixture generator**

`tests/fixtures/e2e/make_bars.py`:

```python
"""Writes the tiny synthetic NQ bar set the Playwright flows run on.

Run from the repo root: uv run python tests/fixtures/e2e/make_bars.py
Two 5-day windows a year apart, so the default 12-month hold-out still leaves a tuning period.
"""

import random
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fenrir.core.bars import Bar
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars

NY = ZoneInfo("America/New_York")
DAYS = [date(2024, 6, d) for d in (3, 4, 5, 6, 7)] + [date(2025, 6, d) for d in (2, 3, 4, 5, 6)]
OUT = Path("tests/fixtures/e2e")


def day_bars(day: date, contract: str, rng: random.Random, price: float) -> tuple[list[Bar], float]:
    start = datetime.combine(day - timedelta(days=1), time(18), NY).astimezone(UTC)
    bars = []
    for m in range(23 * 60):  # 18:00 ET the evening before to 17:00 ET
        o = price
        c = o + rng.choice((-1, 1)) * 0.25 * rng.randint(0, 8)
        hi = max(o, c) + 0.25 * rng.randint(0, 4)
        lo = min(o, c) - 0.25 * rng.randint(0, 4)
        bars.append(Bar(start + timedelta(minutes=m), o, hi, lo, c, rng.randint(50, 500), contract))
        price = c
    return bars, price


def main() -> None:
    instruments = load_instruments()
    rng = random.Random(42)
    price = 18000.0
    by_contract: dict[str, list[Bar]] = defaultdict(list)
    for day in DAYS:
        contract = front_month(instruments, "NQ", day)
        bars, price = day_bars(day, contract, rng, price)
        by_contract[contract].extend(bars)
    for contract, bars in by_contract.items():
        write_bars(OUT, "NQ", contract, bars)
        print(f"{contract}: {len(bars)} bars")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Let the fixture bars be committed and keep run output out of git**

Append to `.gitignore`:

```gitignore
# Playwright fixture data: bars are committed, run output is not
!tests/fixtures/e2e/bars/**/*.parquet
tests/fixtures/e2e/results/
tests/fixtures/e2e/jobs/
tests/fixtures/e2e/*.log
tests/fixtures/e2e/*.db
```

- [ ] **Step 3: Generate the bars**

Run: `uv run python tests/fixtures/e2e/make_bars.py && git status --short tests/fixtures/e2e`
Expected: one line per contract, e.g. `NQM24: 6900 bars` and `NQM25: 6900 bars`; git status lists `tests/fixtures/e2e/bars/` (Parquet files and `manifest.json`) as untracked.

- [ ] **Step 4: Write the failing Playwright test**

`web/e2e/backtest.spec.ts`:

```ts
import { expect, test } from '@playwright/test'

test('run a backtest from the UI and open its report', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto('/backtests')
  await page.getByRole('group', { name: 'Strategies' }).getByRole('checkbox', { name: 'strategies/orb.py' }).check()
  await page.getByRole('group', { name: 'Accounts' }).getByRole('checkbox').first().check()
  await page.getByRole('checkbox', { name: 'NQ', exact: true }).check()
  await page.getByRole('button', { name: 'Run' }).click()

  // Status flips to "done" only through /ws/jobs: the job list does not poll.
  const done = page.getByRole('table', { name: 'Recent jobs' }).getByRole('link', { name: 'done' }).first()
  await expect(done).toBeVisible({ timeout: 150_000 })

  await page.getByRole('link', { name: 'orb' }).first().click()
  await expect(page.getByRole('heading', { level: 1, name: /^orb · .+ · NQ$/ })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Hold-out' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Sample equity curves' }).first()).toBeVisible()
})
```

- [ ] **Step 5: Run it**

Run: `npm --prefix web run e2e -- backtest.spec.ts`
Expected: `1 passed`. If it fails at the `done` step, open the job's `title` tooltip (the item error) in the trace: `npx --prefix web playwright show-trace` — most likely cause is the fixture dates falling outside `instruments.yaml`'s rollover calendar (`front_month` raises in Step 3) or `strategies/orb.py` not existing yet.

- [ ] **Step 6: Commit**

```bash
git add .gitignore tests/fixtures/e2e/make_bars.py tests/fixtures/e2e/bars web/e2e/backtest.spec.ts
git commit -m "test(web): add playwright backtest flow on synthetic fixture bars

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
