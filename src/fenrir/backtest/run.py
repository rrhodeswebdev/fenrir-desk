"""Run a backtest: load inputs, fan attempts out over CPU cores, build the report. Shell."""

from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
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

_worker_inputs: tuple | None = None  # set once per worker process by _init_worker


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
    rules = with_dll(load_account(account_id, settings.rules_dir), dll_on)
    bars = load_bars(settings.data_dir, instruments, strategy.market)
    return bars, strategy, rules, contract_spec


def _init_worker(*load_args) -> None:
    global _worker_inputs
    _worker_inputs = _load(*load_args)


def _run_chunk(ranges: tuple[tuple[int, int], ...]) -> list[tuple[tuple[int, int], AttemptResult]]:
    bars, strategy, rules, contract_spec = _worker_inputs
    return [(r, run_attempt(bars, r[0], r[1], strategy, rules, contract_spec)) for r in ranges]


def _run_all(ranges, load_args, on_progress) -> dict[tuple[int, int], AttemptResult]:
    chunks = [tuple(ranges[k : k + CHUNK]) for k in range(0, len(ranges), CHUNK)]
    out: dict[tuple[int, int], AttemptResult] = {}
    with ProcessPoolExecutor(initializer=_init_worker, initargs=load_args) as pool:
        for future in as_completed([pool.submit(_run_chunk, c) for c in chunks]):
            out.update(future.result())
            if on_progress:
                on_progress(len(out), len(ranges))
    return out


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
    bars, strategy, rules, _ = _load(*load_args)
    tuning, holdout = periods(bars, settings.holdout_months)
    run_periods = [tuning, holdout] if include_holdout and holdout else [tuning]
    results = _run_all([r for p in run_periods for r in p.ranges], load_args, on_progress)

    def summarize(p: Period):
        return period_result([results[r] for r in p.ranges], rules, p.start, p.end)

    return build_report(
        strategy=strategy,
        account_id=account_id,
        rules_hash=file_hash(rules_path(account_id, settings.rules_dir)),
        contract=contract,
        data_version=data_version(settings.data_dir),
        holdout_months=settings.holdout_months,
        include_holdout=include_holdout,
        dll_on=dll_on,
        tuning=summarize(tuning),
        holdout=summarize(holdout) if len(run_periods) == 2 else None,
        created_at=datetime.now(UTC),
    )
