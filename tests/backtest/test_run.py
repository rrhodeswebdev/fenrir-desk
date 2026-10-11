from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from backtest_helpers import et, rules_data

from fenrir.backtest import run as run_module
from fenrir.backtest.attempts import periods
from fenrir.backtest.cache import cached_run
from fenrir.backtest.grid import GridItem
from fenrir.backtest.run import (
    AccountChoice,
    load_account_choices,
    net_per_attempt,
    run_backtest,
    worker_cap,
)
from fenrir.core.bars import Bar
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import load_bars, write_bars
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash

ORB = Path(__file__).parents[2] / "strategies" / "orb.py"
ACCOUNT = "lucid-test-50k"
MINUTE = timedelta(minutes=1)


def weekdays(start: date, end: date):
    return [
        start + timedelta(n)
        for n in range((end - start).days + 1)
        if (start + timedelta(n)).weekday() < 5
    ]


def day_bars(day: date, k: int, contract: str) -> list[Bar]:
    """40 one-minute bars from 09:30 ET with an upward zigzag, so ORB trades some days."""
    out, price = [], 20000.0 + 10 * k
    for m in range(40):
        c = price + 0.5 * m + (1.0 if m % 2 else -1.0) * (k % 3)
        out.append(
            Bar(
                et(day, "09:30") + m * MINUTE,
                price,
                max(price, c) + 0.5,
                min(price, c) - 0.5,
                c,
                10,
                contract,
            )
        )
        price = c
    return out


@pytest.fixture(autouse=True)
def fresh_process_state():
    """Each test starts with no shared pool or memoized inputs, and leaves no workers behind."""
    run_module._drop_pool()
    run_module._memo.clear()
    yield
    run_module._drop_pool()
    run_module._memo.clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = replace(
        Settings(), data_dir=tmp_path / "data", rules_dir=tmp_path / "rules", holdout_months=1
    )
    (s.rules_dir / "lucid").mkdir(parents=True)
    (s.rules_dir / "lucid" / "test-50k.yaml").write_text(yaml.safe_dump(rules_data()))
    instruments = load_instruments()
    by_contract: dict[str, list[Bar]] = {}
    for k, day in enumerate(weekdays(date(2025, 8, 4), date(2025, 9, 30))):
        code = front_month(instruments, "NQ", day)
        by_contract.setdefault(code, []).extend(day_bars(day, k, code))
    for code, bars in by_contract.items():
        write_bars(s.data_dir, "NQ", code, bars)
    return s


def expected_periods(settings):
    return periods(load_bars(settings.data_dir, load_instruments(), "NQ"), settings.holdout_months)


def test_backtest_runs_tuning_only(settings):
    r = run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False)
    tuning, _ = expected_periods(settings)
    assert r["periods"]["holdout"] is None
    assert r["periods"]["tuning"]["attempts"] == len(tuning.ranges)
    assert r["periods"]["tuning"]["end"] == tuning.end.isoformat()
    assert (r["strategy"], r["account"], r["contract"]) == ("orb", ACCOUNT, "MNQ")
    assert r["strategy_hash"] == file_hash(ORB)
    assert len(r["key"]) == 64
    assert r["dll_on"] is False  # no accounts.yaml


def test_evaluate_adds_holdout(settings):
    r = run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=True)
    _, holdout = expected_periods(settings)
    assert r["periods"]["holdout"]["attempts"] == len(holdout.ranges)
    assert r["key"] != run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False)["key"]


def test_same_inputs_same_report(settings):
    a = run_backtest(ORB, ACCOUNT, "NQ", settings, include_holdout=True)
    b = run_backtest(ORB, ACCOUNT, "NQ", settings, include_holdout=True)
    assert {**a, "created_at": None} == {**b, "created_at": None}


def test_progress_reaches_total(settings):
    calls = []
    run_backtest(
        ORB,
        ACCOUNT,
        "MNQ",
        settings,
        include_holdout=False,
        on_progress=lambda d, t: calls.append((d, t)),
    )
    total = calls[-1][1]
    assert calls[-1] == (total, total)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)


def test_contract_must_match_strategy_market(settings, monkeypatch):
    def no_pool(*args, **kwargs):
        pytest.fail("a process pool started before the contract was checked")

    monkeypatch.setattr("fenrir.backtest.run.ProcessPoolExecutor", no_pool)
    with pytest.raises(ValueError, match="MES trades ES prices but orb is an NQ strategy"):
        run_backtest(ORB, ACCOUNT, "MES", settings, include_holdout=False)


def test_completion_order_does_not_change_the_report(settings, monkeypatch):
    natural = run_backtest(ORB, ACCOUNT, "NQ", settings, include_holdout=True)
    monkeypatch.setattr("fenrir.backtest.run.CHUNK", 4)  # several chunks, so order can differ
    submitted = []

    def reversed_completion(futures):
        submitted.extend(futures)
        return reversed(futures)  # every chunk is already submitted, so this is a real order

    monkeypatch.setattr("fenrir.backtest.run.as_completed", reversed_completion)
    forced = run_backtest(ORB, ACCOUNT, "NQ", settings, include_holdout=True)
    assert len(submitted) > 1
    assert {**forced, "created_at": None} == {**natural, "created_at": None}


def test_dll_on_reaches_the_workers(settings):
    dll_account = "lucid-test-dll-50k"
    # target 100 lets the winning synthetic ORB days pass; a $1 limit halts them on day one
    rules = {**rules_data(target=100.0, daily_loss=1.0), "account": "test-dll-50k"}
    (settings.rules_dir / "lucid" / "test-dll-50k.yaml").write_text(yaml.safe_dump(rules))
    off = run_backtest(ORB, dll_account, "MNQ", settings, include_holdout=False)
    (settings.data_dir / "accounts.yaml").write_text(f"{dll_account}: {{dll_on: true}}\n")
    on = run_backtest(ORB, dll_account, "MNQ", settings, include_holdout=False)
    assert off["dll_on"] is False
    assert on["dll_on"] is True
    assert off["periods"]["tuning"]["pass_rate"] > 0
    assert on["periods"]["tuning"]["pass_rate"] == 0


def test_dll_on_without_amount_refuses_to_run(settings):
    (settings.data_dir / "accounts.yaml").write_text(
        f"{ACCOUNT}: {{dll_on: true}}\n"
    )  # rules_data() has no DLL
    with pytest.raises(ValueError, match="no daily_loss_limit amount"):
        run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False)


def test_account_choices_missing_file_is_empty(tmp_path):
    assert load_account_choices(tmp_path) == {}


def test_account_choices_are_read_with_defaults(tmp_path):
    (tmp_path / "accounts.yaml").write_text(
        "lucid-flex-50k: {eval_price: 49.5, dll_on: true}\nlucid-pro-50k: {}\n"
    )
    assert load_account_choices(tmp_path) == {
        "lucid-flex-50k": AccountChoice(eval_price=49.5, dll_on=True),
        "lucid-pro-50k": AccountChoice(),
    }


@pytest.mark.parametrize(
    "text",
    [
        "- 1\n",
        "lucid-flex-50k: 5\n",
        "lucid-flex-50k: {eval_price: -1}\n",
        "lucid-flex-50k: {eval_price: .nan}\n",
        "lucid-flex-50k: {eval_price: true}\n",
        "lucid-flex-50k: {dll_on: 1}\n",
        "lucid-flex-50k: {fee: 5}\n",
        "1: {}\n",
        "lucid-flex-50k: [\n",
    ],
)
def test_bad_account_choices_name_the_file(tmp_path, text):
    (tmp_path / "accounts.yaml").write_text(text)
    with pytest.raises(ValueError, match="accounts.yaml"):
        load_account_choices(tmp_path)


def test_net_per_attempt_subtracts_the_accounts_price():
    period = {"expected_gross_per_attempt": 570.0}
    choices = {"lucid-flex-50k": AccountChoice(eval_price=120.0)}
    assert net_per_attempt(period, "lucid-flex-50k", choices) == 450.0
    assert net_per_attempt(period, "lucid-pro-50k", choices) == 570.0  # no entry = price 0


@pytest.fixture
def spy_pool(monkeypatch):
    """A real pool that records its kwargs and every shutdown's cancel_futures."""
    seen: dict = {"kwargs": None, "cancel": [], "pools": 0}

    class Spy(ProcessPoolExecutor):
        def __init__(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            seen["pools"] += 1
            super().__init__(*args, **kwargs)

        def shutdown(self, wait=True, *, cancel_futures=False):
            seen["cancel"].append(cancel_futures)
            super().shutdown(wait, cancel_futures=cancel_futures)

    monkeypatch.setattr("fenrir.backtest.run.ProcessPoolExecutor", Spy)
    return seen


def test_pool_spawns_its_workers(settings, spy_pool):
    run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False)
    assert spy_pool["kwargs"]["mp_context"].get_start_method() == "spawn"


def no_pool_allowed(monkeypatch):
    def no_pool(*args, **kwargs):
        pytest.fail("a process pool started before the inputs were checked")

    monkeypatch.setattr("fenrir.backtest.run.ProcessPoolExecutor", no_pool)


def test_no_bars_names_the_series_and_the_fix(settings, monkeypatch):
    no_pool_allowed(monkeypatch)
    empty = replace(settings, data_dir=settings.data_dir / "empty")
    with pytest.raises(ValueError, match=r"no NQ bars in .*empty; run `fenrir data pull`"):
        run_backtest(ORB, ACCOUNT, "MNQ", empty, include_holdout=False)


def test_params_must_be_json_serializable(settings, tmp_path, monkeypatch):
    no_pool_allowed(monkeypatch)
    strategy = tmp_path / "timed.py"
    strategy.write_text(
        ORB.read_text()
        .replace("from datetime import date", "from datetime import date, time")
        .replace('PARAMS = {"range_min": 15,', 'PARAMS = {"at": time(9, 30), "range_min": 15,')
    )
    with pytest.raises(ValueError, match="timed.*PARAMS.*JSON"):
        run_backtest(strategy, ACCOUNT, "MNQ", settings, include_holdout=False)


def test_a_worker_that_loaded_different_inputs_is_an_error(settings, tmp_path, monkeypatch):
    strategy = tmp_path / "orb.py"
    strategy.write_text(ORB.read_text())
    plan = run_module._plan

    def edited_after_the_parent_loaded(*args):
        out = plan(*args)
        strategy.write_text(strategy.read_text() + "\n# edited\n")
        return out

    monkeypatch.setattr("fenrir.backtest.run._plan", edited_after_the_parent_loaded)
    with pytest.raises(ValueError, match="strategy file changed during the backtest"):
        run_backtest(strategy, ACCOUNT, "MNQ", settings, include_holdout=False)


def test_a_strategy_broken_after_the_parent_loaded_is_an_error(settings, tmp_path, monkeypatch):
    strategy = tmp_path / "orb.py"
    strategy.write_text(ORB.read_text())
    plan = run_module._plan

    def broken_after_the_parent_loaded(*args):
        out = plan(*args)
        strategy.write_text("def oops(:\n")
        return out

    monkeypatch.setattr("fenrir.backtest.run._plan", broken_after_the_parent_loaded)
    with pytest.raises(ValueError, match="could not load the inputs"):
        run_backtest(strategy, ACCOUNT, "MNQ", settings, include_holdout=False)


def test_a_worker_error_cancels_the_queued_chunks(settings, tmp_path, monkeypatch, spy_pool):
    test_a_worker_that_loaded_different_inputs_is_an_error(settings, tmp_path, monkeypatch)
    assert True in spy_pool["cancel"]


@pytest.mark.parametrize(
    ("changed", "label"),
    [(0, "strategy file"), (1, "rules file"), (2, "bar data"), (3, "bar count")],
)
def test_each_changed_input_is_named(settings, changed, label):
    load_args = (ORB, ACCOUNT, "MNQ", settings, False)
    expected = list(run_module._load(*load_args)[-1])
    run_module._run_chunk(load_args, tuple(expected), ())  # untampered: passes
    expected[changed] = "tampered" if changed < 3 else -1
    with pytest.raises(ValueError, match=f"{label} changed during the backtest"):
        run_module._run_chunk(load_args, tuple(expected), ())


def counted_loads(monkeypatch) -> list[str]:
    calls = []

    def counted(data_dir, instruments, series, *a, **k):
        calls.append(series)
        return load_bars(data_dir, instruments, series, *a, **k)

    monkeypatch.setattr("fenrir.backtest.run.load_bars", counted)
    return calls


def test_a_grid_loads_each_series_once_in_the_parent(settings, monkeypatch, spy_pool):
    loads = counted_loads(monkeypatch)  # spawned workers import their own, uncounted
    for contract in ("MNQ", "NQ"):
        cached_run(GridItem(ORB, ACCOUNT, contract), settings, include_holdout=False)
    assert loads == ["NQ"]
    assert spy_pool["pools"] == 1  # the second item reused the workers
    day = date(2025, 10, 1)
    code = front_month(load_instruments(), "NQ", day)
    write_bars(settings.data_dir, "NQ", code, day_bars(day, 0, code))  # a new data_version
    run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False)
    assert loads == ["NQ", "NQ"]  # a new data_version loads again


def test_a_worker_loads_each_series_once_across_items(settings, monkeypatch):
    loads = counted_loads(monkeypatch)
    for contract in ("MNQ", "NQ"):  # what a reused worker sees for two grid items
        load_args = (ORB, ACCOUNT, contract, settings, False)
        seen = run_module._load(*load_args)[-1]
        run_module._run_chunk(load_args, seen, ())
    assert loads == ["NQ"]


FULL_NQ = 3_386_345  # bars in the full NQ series, Open Question 5
GIB = 1 << 30


@pytest.mark.parametrize(
    ("memory", "cpus", "bars", "workers"),
    [
        (6 * GIB, 16, FULL_NQ, 2),  # the parent holds the series too: 3 processes fit
        (64 * GIB, 16, FULL_NQ, 16),  # never more than the cores
        (2 * GIB, 16, FULL_NQ, 1),  # always at least one
        (6 * GIB, 4, 1_000, 4),  # small data fits every core
        (None, 16, FULL_NQ, 2),  # unknown memory: conservative
        (None, 1, FULL_NQ, 1),
    ],
)
def test_worker_cap_fits_memory(memory, cpus, bars, workers):
    assert worker_cap(memory, cpus, bars) == workers


@pytest.mark.parametrize(("text", "limit"), [("6442450944\n", 6 * GIB), ("max\n", None)])
def test_memory_limit_prefers_the_cgroup(tmp_path, monkeypatch, text, limit):
    cgroup = tmp_path / "memory.max"
    cgroup.write_text(text)
    monkeypatch.setattr(run_module, "CGROUP_MEMORY_MAX", cgroup)
    monkeypatch.setattr(run_module.os, "sysconf", lambda name: 1024)
    assert run_module._memory_limit() == (limit or 1024 * 1024)
