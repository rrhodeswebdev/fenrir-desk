"""Run a backtest: load inputs, fan attempts out over CPU cores, build the report. Shell."""

import json
import multiprocessing
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from fenrir.backtest.attempt import AttemptResult, run_attempt
from fenrir.backtest.attempts import Period, periods
from fenrir.backtest.report import PeriodResult, Report, build_report, period_result
from fenrir.core.instruments import load_instruments, spec
from fenrir.data.store import data_version, load_bars
from fenrir.rules.account import with_dll
from fenrir.rules.loader import load_account, rules_path
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash, load_strategy

CHUNK = 16  # attempts per task: amortizes pickling without starving cores
# measured (SPEC Open Question 5): a process holding the full 3.4M-bar NQ series is about 1.9 GB
PROCESS_BYTES = 200_000_000  # interpreter, polars and the fenrir imports
BYTES_PER_BAR = 520
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")

# per process: name -> (key, value). One value per name, so a new series replaces the old one
_memo: dict[str, tuple] = {}
_pool: tuple[tuple, ProcessPoolExecutor] | None = None  # ((workers, cwd), pool), kept across runs


class AccountChoice(BaseModel):
    """The user's own choices for one account type (`<data_dir>/accounts.yaml`), not firm rules."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)
    eval_price: float = Field(0.0, ge=0)  # what the user paid; varies with coupons
    dll_on: bool = False  # Lucid's daily loss limit turned on for this account


_CHOICES = TypeAdapter(dict[str, AccountChoice])


def load_account_choices(data_dir: Path) -> dict[str, AccountChoice]:
    """Absent file, account or key = the defaults (price 0, DLL off)."""
    path = data_dir / "accounts.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
        return {} if data is None else _CHOICES.validate_python(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise ValueError(f"{path}: {e}") from e


def net_per_attempt(
    period: PeriodResult, account_id: str, choices: Mapping[str, AccountChoice]
) -> float:
    return (
        period["expected_gross_per_attempt"] - choices.get(account_id, AccountChoice()).eval_price
    )


def worker_cap(memory: int | None, cpus: int, bars: int) -> int:
    """Workers that fit in `memory` bytes beside the parent, which holds the same bars. Pure."""
    if memory is None:
        return min(2, cpus)  # unknown limit: stay small rather than swap
    per_process = PROCESS_BYTES + BYTES_PER_BAR * bars
    return max(1, min(cpus, memory // per_process - 1))


def _memory_limit() -> int | None:
    """The container's cgroup limit when there is one, else physical memory; None if unknown."""
    try:
        text = CGROUP_MEMORY_MAX.read_text().strip()
        if text != "max":
            return int(text)
    except (OSError, ValueError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return None


def _once(name: str, key, make: Callable):
    """make() once per process per key; the old value is dropped first, so two never coexist."""
    if name not in _memo or _memo[name][0] != key:
        _memo.pop(name, None)
        _memo[name] = (key, make())
    return _memo[name][1]


def _load(
    strategy_path: Path, account_id: str, contract: str, settings: Settings, dll_on: bool
) -> tuple:
    strategy = load_strategy(strategy_path)
    instruments = load_instruments()
    contract_spec = spec(instruments, contract)
    if contract_spec.series != strategy.market:
        raise ValueError(
            f"{contract} trades {contract_spec.series} prices but {strategy.name} "
            f"is an {strategy.market} strategy"
        )
    # hashed before the files are read, so the report names at most what was read
    rules_hash = file_hash(rules_path(account_id, settings.rules_dir))
    version = data_version(settings.data_dir)
    rules = with_dll(load_account(account_id, settings.rules_dir), dll_on)
    bars = _once(
        "bars",
        (settings.data_dir.resolve(), strategy.market, version),
        lambda: load_bars(settings.data_dir, instruments, strategy.market),
    )
    seen = (strategy.hash, rules_hash, version, len(bars))
    return bars, strategy, rules, contract_spec, seen


def _run_chunk(
    load_args: tuple, expected: tuple, ranges: tuple[tuple[int, int], ...]
) -> list[tuple[tuple[int, int], AttemptResult]]:
    # workers outlive a run: a later grid item reloads strategy and rules, and reuses the bars
    try:
        bars, strategy, rules, contract_spec, seen = _once(
            "inputs", (load_args, expected), lambda: _load(*load_args)
        )
    except Exception as e:
        raise ValueError(f"a worker could not load the inputs (did a file change?): {e}") from e
    for what, want, got in zip(
        ("strategy file", "rules file", "bar data", "bar count"), expected, seen, strict=True
    ):
        if want != got:
            raise ValueError(f"{what} changed during the backtest; run it again")
    # fills are not in the report; dropping them saves pickling every fill back to the parent
    return [
        (r, replace(run_attempt(bars, r[0], r[1], strategy, rules, contract_spec), fills=()))
        for r in ranges
    ]


def _drop_pool() -> None:
    global _pool
    if _pool:
        _pool[1].shutdown(cancel_futures=True)  # fail fast: don't run the queued chunks
    _pool = None


def _shared_pool(workers: int) -> ProcessPoolExecutor:
    global _pool
    key = (workers, os.getcwd())  # spawned workers keep the cwd they started in
    if _pool is None or _pool[0] != key:
        _drop_pool()
        # spawn: fork can deadlock polars in the children, and Linux defaults to it
        pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        )
        _pool = (key, pool)
    return _pool[1]


def _run_all(
    ranges, load_args, expected, workers, on_progress
) -> dict[tuple[int, int], AttemptResult]:
    chunks = [tuple(ranges[k : k + CHUNK]) for k in range(0, len(ranges), CHUNK)]
    out: dict[tuple[int, int], AttemptResult] = {}
    try:
        pool = _shared_pool(workers)
        for future in as_completed(
            [pool.submit(_run_chunk, load_args, expected, c) for c in chunks]
        ):
            out.update(future.result())
            if on_progress:
                on_progress(len(out), len(ranges))
    except BaseException:
        _drop_pool()  # the next run starts fresh workers
        raise
    return out


def _plan(load_args, holdout_months: int):
    """Load and split into periods; the bars and periods stay memoized for the next grid item."""
    bars, strategy, rules, _, seen = _load(*load_args)
    if not bars:
        raise ValueError(
            f"no {strategy.market} bars in {load_args[3].data_dir}; run `fenrir data pull`"
        )
    tuning, holdout = _once(
        "periods", (_memo["bars"][0], holdout_months), lambda: periods(bars, holdout_months)
    )
    return strategy, rules, seen, tuning, holdout


def run_backtest(
    strategy_path: Path,
    account_id: str,
    contract: str,
    settings: Settings,
    *,
    include_holdout: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> Report:
    dll_on = load_account_choices(settings.data_dir).get(account_id, AccountChoice()).dll_on
    load_args = (
        strategy_path,
        account_id,
        contract,
        settings,
        dll_on,
    )  # workers get the same choice
    strategy, rules, seen, tuning, holdout = _plan(load_args, settings.holdout_months)
    try:
        json.dumps(dict(strategy.params))
    except TypeError as e:
        raise ValueError(f"{strategy.name}: PARAMS must be JSON-serializable ({e})") from e
    run_periods = [tuning, holdout] if include_holdout and holdout else [tuning]
    results = _run_all(
        [r for p in run_periods for r in p.ranges],
        load_args,
        seen,
        worker_cap(_memory_limit(), os.cpu_count() or 1, seen[3]),
        on_progress,
    )

    def summarize(p: Period):
        return period_result([results[r] for r in p.ranges], rules, p.start, p.end)

    return build_report(
        strategy=strategy,
        account_id=account_id,
        rules_hash=seen[1],
        contract=contract,
        data_version=seen[2],
        holdout_months=settings.holdout_months,
        include_holdout=include_holdout,
        dll_on=dll_on,
        tuning=summarize(tuning),
        holdout=summarize(holdout) if len(run_periods) == 2 else None,
        created_at=datetime.now(UTC),
    )
