"""Run a backtest: load inputs, fan attempts out over CPU cores, build the report. Shell."""

import json
import multiprocessing
import os
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from fenrir.backtest.attempt import AttemptResult, run_attempt
from fenrir.backtest.attempts import Period, periods
from fenrir.backtest.report import PeriodResult, Report, build_report, period_result
from fenrir.core.instruments import load_instruments, spec
from fenrir.data.store import data_version, load_bars
from fenrir.files import read_regular
from fenrir.rules.account import with_dll
from fenrir.rules.loader import load_account, rules_path
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash, load_strategy

CHUNK = 16  # attempts per task: amortizes pickling without starving cores
# measured (SPEC Open Question 5): a worker holding the full 3.4M-bar NQ series is about 1.9 GB
PROCESS_BYTES = 200_000_000  # interpreter, polars and the fenrir imports
BYTES_PER_BAR = 520
# of the memory available at start, which already leaves out other processes (a VM, #260): the
# rest is for load peaks, the page cache MemAvailable counts as free, and others growing later
BUDGET_PCT = 70
# cgroup v2 only: a v1 limit is not read. In a container (private cgroupns) the process's cgroup
# is the root; natively it is e.g. a `systemd-run -p MemoryMax=` scope under the root
CGROUP_ROOT = Path("/sys/fs/cgroup")
SELF_CGROUP = Path("/proc/self/cgroup")
MEMINFO = Path("/proc/meminfo")
WORKERS_ENV = "FENRIR_BACKTEST_WORKERS"  # process env only, like FENRIR_PTY_USER: a cap
_warned: set[str] = set()  # invalid WORKERS_ENV values already warned about, once per process

# per process: name -> (key, value). One value per name, so a new series replaces the old one
_memo: dict[str, tuple] = {}
_pool: tuple[tuple, ProcessPoolExecutor] | None = None  # ((workers, cwd), pool), kept across runs
_lock = threading.Lock()  # one run at a time per process: the pool and memo are shared


class AccountChoice(BaseModel):
    """The user's own choices for one account type (`<data_dir>/accounts.yaml`), not firm rules."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)
    eval_price: float = Field(0.0, ge=0)  # what the user paid; varies with coupons
    dll_on: bool = False  # Lucid's daily loss limit turned on for this account


_CHOICES = TypeAdapter(dict[str, AccountChoice])
ACCOUNTS_MAX = 1 << 20


def load_account_choices(data_dir: Path) -> dict[str, AccountChoice]:
    """Absent file, account or key = the defaults (price 0, DLL off)."""
    path = data_dir / "accounts.yaml"
    try:
        # the terminal can write data/: no symlink, FIFO or huge file, read as root (DEC-1)
        text = read_regular(path, ACCOUNTS_MAX)
    except FileNotFoundError:
        return {}
    # errors name the file, line and field but never quote it: the text may not be ours
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as e:  # deep nesting overflows the parser
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"{path}: not valid YAML{where}") from None
    try:
        return {} if data is None else _CHOICES.validate_python(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise ValueError(f"{path}: {problems}") from None


def net_per_attempt(
    period: PeriodResult, account_id: str, choices: Mapping[str, AccountChoice]
) -> float:
    return (
        period["expected_gross_per_attempt"] - choices.get(account_id, AccountChoice()).eval_price
    )


def worker_cap(memory: int | None, cpus: int, bars: int) -> tuple[int, int]:
    """(workers, bytes one worker lacks) within BUDGET_PCT of `memory`, beside the parent.

    The parent keeps only periods, so it counts as PROCESS_BYTES. Never fewer than 1 worker. Pure.
    """
    if memory is None:
        return min(2, cpus), 0  # unknown limit: stay small rather than swap
    # parent counted as PROCESS_BYTES (0.2 GB); measured 0.79 GB after a load, within the headroom
    free = memory * BUDGET_PCT // 100 - PROCESS_BYTES
    per_worker = PROCESS_BYTES + BYTES_PER_BAR * bars
    return max(1, min(cpus, free // per_worker)), max(0, per_worker - free)


def mem_available(text: str) -> int | None:
    """Bytes of `MemAvailable` in /proc/meminfo text (in kB there); None when absent. Pure."""
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name == "MemAvailable":
            value = rest.split()[:1]
            return int(value[0]) * 1024 if value and value[0].isdecimal() else None
    return None


def cgroup_dirs(text: str, root: Path) -> list[Path]:
    """The process's cgroup v2 directory under `root`, then each parent up to `root`, from
    /proc/self/cgroup text; just [root] without a usable `0::` line (cgroup v1). Pure."""
    for line in text.splitlines():
        if line.startswith("0::/"):
            parts = PurePosixPath(line[3:]).parts[1:]  # parts[0] is "/"
            if ".." not in parts:
                return [root.joinpath(*parts[:n]) for n in range(len(parts), -1, -1)]
    return [root]


def _memory_max(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
        return None if text == "max" else int(text)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None


def _cgroup_limit(root: Path, self_cgroup: Path) -> int | None:
    """The smallest `memory.max` from this process's cgroup up to the root; None if none is set."""
    try:
        text = self_cgroup.read_text()
    except (OSError, ValueError):
        text = ""
    limits = (_memory_max(d / "memory.max") for d in cgroup_dirs(text, root))
    return min((m for m in limits if m is not None), default=None)


def _available(path: Path) -> int | None:
    try:
        return mem_available(path.read_text())
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None


def _memory_limit(
    cgroup_root: Path = CGROUP_ROOT, self_cgroup: Path = SELF_CGROUP, meminfo: Path = MEMINFO
) -> int | None:
    """Memory available at start: the smaller of the cgroup limit and MemAvailable (in a container
    MemAvailable is the host's), else physical memory; None if unknown."""
    cgroup = _cgroup_limit(cgroup_root, self_cgroup)
    known = [m for m in (cgroup, _available(meminfo)) if m is not None]
    if known:
        return min(known)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return None


def _workers_cap(env: Mapping[str, str]) -> int | None:
    """`FENRIR_BACKTEST_WORKERS` as a positive int; unset or empty is None, anything else warns."""
    raw = env.get(WORKERS_ENV, "")
    if raw == "":
        return None
    try:
        cap = int(raw)
    except ValueError:
        cap = 0
    if cap >= 1:
        return cap
    if raw not in _warned:  # once per process, not once per grid item
        _warned.add(raw)
        print(
            f"warning: ignoring {WORKERS_ENV}={raw!r}: not a positive whole number", file=sys.stderr
        )
    return None


def _workers(bars: int, memory: int | None, env: Mapping[str, str] = os.environ) -> int:
    workers, short = worker_cap(memory, os.cpu_count() or 1, bars)
    if short:
        print(
            f"warning: one backtest worker for {bars:,} bars needs about {short / 1e9:.1f} GB more "
            f"than {BUDGET_PCT}% of the {memory / 1e9:.1f} GB of memory available; "
            "running 1 anyway",
            file=sys.stderr,
        )
    cap = _workers_cap(env)
    return workers if cap is None else min(workers, cap)


def _once(name: str, key, make: Callable):
    """make() once per process per key; the old value is dropped first, so two never coexist."""
    if name not in _memo or _memo[name][0] != key:
        _memo.pop(name, None)
        _memo[name] = (key, make())
    return _memo[name][1]


def release() -> None:
    """Shut the shared workers down and forget the memoized inputs, freeing their memory.

    Takes the run lock, which is not reentrant: never call it from an `on_progress` callback.
    """
    with _lock:
        _drop_pool()
        _memo.clear()


def _load(
    strategy_path: Path, account_id: str, contract: str, settings: Settings, dll_on: bool
) -> tuple:
    """Everything but the bars, plus the key the bars are loaded (and memoized) by."""
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
    bars_key = (settings.data_dir.resolve(), strategy.market, version, instruments)
    return strategy, rules, contract_spec, rules_hash, bars_key


def _load_bars(bars_key: tuple) -> tuple:
    data_dir, series, _, instruments = bars_key
    return load_bars(data_dir, instruments, series)


def _worker_inputs(load_args: tuple) -> tuple:
    strategy, rules, contract_spec, rules_hash, bars_key = _load(*load_args)
    bars = _once("bars", bars_key, lambda: _load_bars(bars_key))
    return bars, strategy, rules, contract_spec, (strategy.hash, rules_hash, bars_key[2], len(bars))


def _run_chunk(
    load_args: tuple, expected: tuple, ranges: tuple[tuple[int, int], ...]
) -> list[tuple[tuple[int, int], AttemptResult]]:
    # workers outlive a run: a later grid item reloads strategy and rules, and reuses the bars
    try:
        bars, strategy, rules, contract_spec, seen = _once(
            "inputs", (load_args, expected, load_instruments()), lambda: _worker_inputs(load_args)
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
    """Load and split into periods; only the periods and bar count stay memoized, not the bars."""
    strategy, rules, _, rules_hash, bars_key = _load(*load_args)

    def split():
        _drop_pool()  # idle workers hold the old series: free it before the parent's load peak
        # measured once per series, with no workers alive and before the load: live workers
        # holding the bars would shrink MemAvailable, and a smaller count a new pool (#260)
        memory = _memory_limit()
        bars = _load_bars(bars_key)
        if not bars:
            raise ValueError(
                f"no {strategy.market} bars in {load_args[3].data_dir}; run `fenrir data pull`"
            )
        return *periods(bars, holdout_months), len(bars), memory

    tuning, holdout, count, memory = _once("plan", (bars_key, holdout_months), split)
    seen = (strategy.hash, rules_hash, bars_key[2], count)
    return strategy, rules, seen, tuning, holdout, memory


def run_backtest(
    strategy_path: Path,
    account_id: str,
    contract: str,
    settings: Settings,
    *,
    include_holdout: bool,
    on_progress: Callable[[int, int], None] | None = None,
) -> Report:
    """Runs are serialized per process: a second thread waits for the first to finish.

    The process keeps a spawn pool and memoized inputs afterwards; `release()` frees them.
    """
    dll_on = load_account_choices(settings.data_dir).get(account_id, AccountChoice()).dll_on
    load_args = (
        strategy_path,
        account_id,
        contract,
        settings,
        dll_on,
    )  # workers get the same choice
    with _lock:
        strategy, rules, seen, tuning, holdout, memory = _plan(load_args, settings.holdout_months)
        try:
            json.dumps(dict(strategy.params))
        except TypeError as e:
            raise ValueError(f"{strategy.name}: PARAMS must be JSON-serializable ({e})") from e
        run_periods = [tuning, holdout] if include_holdout and holdout else [tuning]
        results = _run_all(
            [r for p in run_periods for r in p.ranges],
            load_args,
            seen,
            _workers(seen[3], memory),
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
