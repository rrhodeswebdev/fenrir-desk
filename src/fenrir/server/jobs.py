"""Pure backtest-job state (`Job` JSON in docs/contracts.md, Plan 14). The runner does the IO."""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from fenrir.backtest.grid import GridItem

Status = Literal["pending", "running", "done", "error"]
KEY = re.compile(r"[0-9a-f]{64}")  # report_key's sha256 hex; it becomes a file name later


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
    items = list(job.items)
    items[index] = replace(items[index], **changes)  # a bad index raises, not a silent no-op
    return replace(job, items=tuple(items))


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
    """True when the total or the whole percent changed: at most ~100 updates per item."""
    return old[1] != new[1] or _pct(old) != _pct(new)


def to_json(job: Job) -> dict[str, Any]:
    return asdict(job)


def _item(data: Any) -> JobItem:
    match data:
        case {
            "strategy": str(),
            "account": str(),
            "contract": str(),
            "status": "pending" | "running" | "done" | "error",
            "key": None | str(),
            "error": None | str(),
            "progress": [done, total],
        } if (
            len(data) == 7
            and type(done) is int  # not bool
            and type(total) is int
            and (data["key"] is None or KEY.fullmatch(data["key"]))
        ):
            return JobItem(**{**data, "progress": (done, total)})
    raise ValueError(f"not a job item: {data!r}"[:300])


def from_json(data: Any) -> Job:
    """ValueError for anything `to_json` would not write: job files are read back from disk."""
    match data:
        case {"id": str(), "created_at": str(), "include_holdout": bool(), "items": list()} if (
            len(data) == 4
        ):
            items = tuple(_item(i) for i in data["items"])
            return Job(data["id"], data["created_at"], data["include_holdout"], items)
    raise ValueError("not a job")
