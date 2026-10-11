"""Backtest reports cached in <data_dir>/results/<key>.json. Shell."""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from fenrir.backtest.grid import GridItem
from fenrir.backtest.report import Report, report_key
from fenrir.backtest.run import AccountChoice, load_account_choices, run_backtest
from fenrir.data.store import data_version
from fenrir.rules.loader import rules_path
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash


def _results(settings: Settings) -> Path:
    return settings.data_dir / "results"


def cache_key(item: GridItem, settings: Settings, include_holdout: bool) -> str:
    choice = load_account_choices(settings.data_dir).get(item.account_id, AccountChoice())
    return report_key(
        file_hash(item.strategy_path),
        file_hash(rules_path(item.account_id, settings.rules_dir)),
        data_version(settings.data_dir),
        item.contract,
        settings.holdout_months,
        include_holdout,
        choice.dll_on,  # eval_price is never in the key
    )


def cached_run(
    item: GridItem,
    settings: Settings,
    *,
    include_holdout: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> Report:
    path = _results(settings) / f"{cache_key(item, settings, include_holdout)}.json"
    if path.exists():
        return json.loads(path.read_text())
    report = run_backtest(
        item.strategy_path,
        item.account_id,
        item.contract,
        settings,
        include_holdout=include_holdout,
        on_progress=on_progress,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # temp + rename: a crash never leaves a half-written <key>.json to be read as a hit;
    # a unique temp name keeps two runs of the same key from writing into one file
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(report, f)
    # stored under the key of the bytes that ran: a file edited mid-run misses next time
    os.replace(tmp, path.parent / f"{report['key']}.json")
    return report


def list_reports(settings: Settings) -> tuple[Report, ...]:
    reports = [json.loads(p.read_text()) for p in _results(settings).glob("*.json")]
    return tuple(sorted(reports, key=lambda r: r["created_at"], reverse=True))
