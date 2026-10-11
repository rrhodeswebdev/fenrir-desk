"""Run a backtest: load inputs, fan attempts out over CPU cores, build the report. Shell."""

import json
import multiprocessing
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

_worker_inputs: tuple | Exception | None = None  # set once per worker process by _init_worker


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
    bars = load_bars(settings.data_dir, instruments, strategy.market)
    seen = (strategy.hash, rules_hash, version, len(bars))
    return bars, strategy, rules, contract_spec, seen


def _init_worker(*load_args) -> None:
    global _worker_inputs
    try:
        _worker_inputs = _load(*load_args)
    except Exception as e:  # raised from _run_chunk instead: see there
        _worker_inputs = e


def _run_chunk(
    expected: tuple, ranges: tuple[tuple[int, int], ...]
) -> list[tuple[tuple[int, int], AttemptResult]]:
    # here, not in the initializer: a raise there would surface as a BrokenProcessPool
    if isinstance(_worker_inputs, Exception):
        raise ValueError(
            f"a worker could not load the inputs (did a file change?): {_worker_inputs}"
        )
    bars, strategy, rules, contract_spec, seen = _worker_inputs
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


def _run_all(ranges, load_args, expected, on_progress) -> dict[tuple[int, int], AttemptResult]:
    chunks = [tuple(ranges[k : k + CHUNK]) for k in range(0, len(ranges), CHUNK)]
    out: dict[tuple[int, int], AttemptResult] = {}
    # spawn: fork can deadlock polars in the children, and Linux defaults to it
    with ProcessPoolExecutor(
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=load_args,
    ) as pool:
        try:
            for future in as_completed([pool.submit(_run_chunk, expected, c) for c in chunks]):
                out.update(future.result())
                if on_progress:
                    on_progress(len(out), len(ranges))
        except BaseException:
            pool.shutdown(cancel_futures=True)  # fail fast: don't run the queued chunks
            raise
    return out


def _plan(load_args, holdout_months: int):
    """Load, split into periods, and return only what the parent needs, so the bars are freed."""
    bars, strategy, rules, _, seen = _load(*load_args)
    if not bars:
        raise ValueError(
            f"no {strategy.market} bars in {load_args[3].data_dir}; run `fenrir data pull`"
        )
    tuning, holdout = periods(bars, holdout_months)
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
