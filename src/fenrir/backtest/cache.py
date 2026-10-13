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
from fenrir.files import read_regular
from fenrir.rules.loader import rules_path
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash

REPORT_MAX = 16 * 2**20


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


def _read(path: Path) -> Report:
    # the terminal can write data/results: no symlink, FIFO or huge file, read as root (DEC-1)
    data = read_regular(path, REPORT_MAX)
    try:
        report = json.loads(data)
        report["created_at"], report["key"]  # noqa: B018 - list_reports sorts on both
        report["strategy_hash"], report["account"]  # noqa: B018 - the API matches and nets on both
        if not all(p is None or isinstance(p, dict) for p in report["periods"].values()):
            raise TypeError("a period is not an object or null")
        return report
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"{path}: corrupt cached report, delete it ({e!r})") from e


def cached_run(
    item: GridItem,
    settings: Settings,
    *,
    include_holdout: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> Report:
    path = _results(settings) / f"{cache_key(item, settings, include_holdout)}.json"
    if path.exists():
        return _read(path)
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
    text = json.dumps(report)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # no empty <key>.json after a power loss
        # stored under the key of the bytes that ran: a file edited mid-run misses next time
        os.replace(tmp, path.parent / f"{report['key']}.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return json.loads(text)  # same types as a later hit (tuples come back as lists)


def read_report(settings: Settings, key: str) -> Report:
    """The cached report `key` (FileNotFoundError when absent); the caller vets `key`."""
    return _read(_results(settings) / f"{key}.json")


def list_reports(settings: Settings, limit: int | None = None) -> tuple[Report, ...]:
    """Every cached report, or only the `limit` most recently written files (mtime, then name)."""
    paths = list(_results(settings).glob("*.json"))
    if limit is not None:  # only these are read: an older corrupt file never fails the call
        paths = sorted(paths, key=lambda p: (p.lstat().st_mtime_ns, p.name), reverse=True)[:limit]
    reports = [_read(p) for p in paths]
    # key breaks created_at ties (one-second resolution) so the order is the same everywhere
    return tuple(sorted(reports, key=lambda r: (r["created_at"], r["key"]), reverse=True))
