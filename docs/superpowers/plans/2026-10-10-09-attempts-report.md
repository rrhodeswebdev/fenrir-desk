# Attempts and Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-attempt fold into a backtest: one attempt starting on every trading day, run in parallel across CPU cores, split into a tuning period and a held-out period, summarized as the report JSON, and exposed as `fenrir backtest` (tuning only) and `fenrir evaluate` (tuning + hold-out). Then measure whether it is fast enough.

**Architecture:** `attempts.py` (pure) finds each trading day's first bar and splits days into tuning and hold-out `Period`s, each a tuple of `(start, end)` bar ranges; tuning ranges end where the hold-out begins, so tuning attempts never see held-out bars. `report.py` (pure) turns `AttemptResult`s into `PeriodResult`s and a `Report`. `run.py` is the shell: it loads files, fans the ranges out over a `ProcessPoolExecutor` whose workers each load their own copy of the inputs once, and assembles the report. `cli/backtest.py` is a thin Typer wrapper.

**Tech Stack:** Python 3.12, `concurrent.futures.ProcessPoolExecutor`, Typer, PyYAML (tests), pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§6 Attempts, Report, Hold-out, Performance)

**Index:** `docs/superpowers/plans/README.md` (contracts: plans 01, 03, 05, 06, 07, 09 `run_backtest` and `Report` JSON)

## Global Constraints

- Python 3.12, `uv`; `uv run pytest -q`; `uv run ruff check . && uv run ruff format --check .`.
- Functional core, imperative shell: `attempts.py` and `report.py` are pure; only `run.py` and `cli/backtest.py` do I/O. The process pool lives only in `run.py`.
- One attempt starts on **every trading day** in a period (spec §6). Attempts are independent pure functions of their start; results are ordered by start date, never by completion order.
- Hold-out: the most recent `Settings.holdout_months` (default 12) months of data. `fenrir backtest` never runs it; tuning attempts never see its bars. `fenrir evaluate` and UI runs produce both periods.
- `holdout_gap` = held-out `p_pass_and_payout` < 50% of tuning's.
- Report JSON shape and `run_backtest` signature exactly as in the index (`README.md` → Plans 09/10).
- `failure_reasons` keys: `"max_drawdown"`, `"daily_loss_limit"`, `"flat_by"` (the only `Breach.rule` values plan 06 produces) and `"no_result"`; counted over every attempt that did not reach a first payout.
- The report always carries the note: "Attempts start on every trading day and overlap, so these rates are estimates, not exact probabilities."
- Commits: Conventional Commits, scope `backtest` (or `cli` for the CLI task), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. `holdout_months` covering all the data → a clear `ValueError` naming the months and data range, not an empty report or `ZeroDivisionError`. Test in Task 1 (`test_holdout_covering_all_data_is_an_error`).
2. `holdout_months = 0` → no hold-out: `evaluate` returns `periods.holdout = null` and `holdout_gap = false`. Tests in Task 1 and Task 2.
3. Strategy/contract mismatch (an `ES` strategy run on `MNQ`) → `ValueError` before any process starts. Test in Task 3 (`test_contract_must_match_strategy_market`).
4. Month arithmetic at month ends (`2026-03-31` minus 1 month, January minus 1 month) → clamped valid dates, not `ValueError: day is out of range`. Test in Task 1.
5. Parallel completion order differs between runs → identical reports apart from `created_at`. Test in Task 3 (`test_same_inputs_same_report`).

---

### Task 1: Attempt starts and periods

**Files:**
- Create: `src/fenrir/backtest/attempts.py`
- Test: `tests/backtest/test_attempts.py`

**Interfaces:**
- Consumes: `Bar`, `trading_day` (plan 01).
- Produces (`fenrir.backtest.attempts`):
  - `Period(start: date, end: date, ranges: tuple[tuple[int, int], ...])` — first/last trading day; one `(start index, end index)` per attempt.
  - `months_before(d: date, months: int) -> date`
  - `day_starts(bars: tuple[Bar, ...]) -> tuple[tuple[date, int], ...]` — first bar index of each trading day.
  - `periods(bars: tuple[Bar, ...], holdout_months: int) -> tuple[Period, Period | None]` — `(tuning, holdout)`; raises `ValueError` if there are no bars or no tuning days.

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_attempts.py`:

```python
from datetime import date, timedelta

import pytest
from backtest_helpers import D1, D2, bar

from fenrir.backtest.attempts import day_starts, months_before, periods


@pytest.mark.parametrize(
    ("d", "n", "expected"),
    [
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2026, 1, 15), 1, date(2025, 12, 15)),
        (date(2026, 10, 10), 12, date(2025, 10, 10)),
        (date(2024, 3, 31), 1, date(2024, 2, 29)),
        (date(2026, 5, 20), 0, date(2026, 5, 20)),
    ],
)
def test_months_before(d, n, expected):
    assert months_before(d, n) == expected


def test_day_starts_use_trading_days():
    bars = (
        bar("09:30", 1, 1, 1, 1, D1),
        bar("09:31", 1, 1, 1, 1, D1),
        bar("18:00", 1, 1, 1, 1, D1),  # 18:00 ET belongs to the next trading day
        bar("09:30", 1, 1, 1, 1, D2),
    )
    assert day_starts(bars) == ((D1, 0), (D2, 2))


def weekday_bars(start: date, end: date):
    days = (start + timedelta(n) for n in range((end - start).days + 1))
    return tuple(bar("09:30", 1, 1, 1, 1, d) for d in days if d.weekday() < 5)


def test_periods_split_on_months():
    bars = weekday_bars(date(2025, 1, 1), date(2025, 6, 30))
    tuning, holdout = periods(bars, 2)
    split = next(i for i, b in enumerate(bars) if b.ts.date() >= date(2025, 5, 1))
    assert (tuning.start, tuning.end) == (date(2025, 1, 1), date(2025, 4, 30))
    assert (holdout.start, holdout.end) == (date(2025, 5, 1), date(2025, 6, 30))
    assert len(tuning.ranges) + len(holdout.ranges) == len(bars)
    assert all(end == split for _, end in tuning.ranges)  # tuning never sees hold-out bars
    assert all(end == len(bars) for _, end in holdout.ranges)
    assert tuning.ranges[0] == (0, split)


def test_zero_months_means_no_holdout():
    bars = weekday_bars(date(2025, 1, 1), date(2025, 1, 31))
    tuning, holdout = periods(bars, 0)
    assert holdout is None
    assert len(tuning.ranges) == len(bars)
    assert all(end == len(bars) for _, end in tuning.ranges)


def test_holdout_covering_all_data_is_an_error():
    with pytest.raises(ValueError, match="hold-out of 12 months covers all data"):
        periods(weekday_bars(date(2025, 1, 1), date(2025, 3, 31)), 12)


def test_no_bars_is_an_error():
    with pytest.raises(ValueError, match="no bars"):
        periods((), 12)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_attempts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest.attempts'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/backtest/attempts.py`:

```python
"""Where attempts start and which bars each may see. Pure."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from fenrir.core.bars import Bar
from fenrir.core.sessions import trading_day


@dataclass(frozen=True, slots=True)
class Period:
    start: date  # first trading day
    end: date  # last trading day
    ranges: tuple[tuple[int, int], ...]  # (first bar, end bar) per attempt, one per trading day


def months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def day_starts(bars: tuple[Bar, ...]) -> tuple[tuple[date, int], ...]:
    out, last = [], None
    for i, b in enumerate(bars):
        day = trading_day(b.ts)
        if day != last:
            out.append((day, i))
            last = day
    return tuple(out)


def periods(bars: tuple[Bar, ...], holdout_months: int) -> tuple[Period, Period | None]:
    starts = day_starts(bars)
    if not starts:
        raise ValueError("no bars")
    first, last = starts[0][0], starts[-1][0]
    cutoff = months_before(last, holdout_months) if holdout_months > 0 else last
    tuning = [s for s in starts if s[0] <= cutoff]
    holdout = [s for s in starts if s[0] > cutoff]
    if not tuning:
        raise ValueError(
            f"hold-out of {holdout_months} months covers all data ({first} to {last})"
        )
    split = holdout[0][1] if holdout else len(bars)
    return _period(tuning, split), _period(holdout, len(bars)) if holdout else None


def _period(starts: list[tuple[date, int]], end: int) -> Period:
    return Period(starts[0][0], starts[-1][0], tuple((i, end) for _, i in starts))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_attempts.py -q`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/backtest/attempts.py tests/backtest/test_attempts.py
git commit -m "$(cat <<'EOF'
feat(backtest): start one attempt per trading day and split hold-out

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Report JSON

**Files:**
- Create: `src/fenrir/backtest/report.py`
- Test: `tests/backtest/test_report.py`

**Interfaces:**
- Consumes: `AttemptResult` (plan 08), `AccountRules` (plan 05: `costs.eval_fee`, `costs.activation_fee`), `Strategy` (plan 07), `Breach` (plan 06).
- Produces (`fenrir.backtest.report`):
  - `TypedDict`s `DaysToPass`, `EquitySample`, `PeriodResult`, `Report` — exactly the index's JSON shape.
  - `NOTE: str` — the overlap note.
  - `period_result(results: Sequence[AttemptResult], rules: AccountRules, start: date, end: date) -> PeriodResult` — `results` ordered by start; non-empty.
  - `report_key(strategy_hash: str, rules_hash: str, data_version: str, contract: str, holdout_months: int, include_holdout: bool) -> str` — sha256 hex. Plan 10's `cache_key` calls this.
  - `build_report(*, strategy: Strategy, account_id: str, rules_hash: str, contract: str, data_version: str, holdout_months: int, include_holdout: bool, tuning: PeriodResult, holdout: PeriodResult | None, created_at: datetime) -> Report`.
- Definitions used in `PeriodResult`:
  - `pass_rate` = attempts with `days_to_pass` set / attempts.
  - `payout_rate` = attempts reaching a payout / attempts that passed (0.0 when none passed).
  - `p_pass_and_payout` = attempts reaching a payout / attempts.
  - `expected_net_per_attempt` = mean over attempts of `first_payout (or 0) − eval_fee − (activation_fee if passed)`.
  - Medians are `null` when empty; `days_to_pass.histogram` is `[[days, count], ...]` sorted by days.

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_report.py`:

```python
from datetime import UTC, date, datetime

import pytest
from backtest_helpers import make_rules, scripted

from fenrir.backtest.attempt import AttemptResult
from fenrir.backtest.report import NOTE, build_report, period_result, report_key
from fenrir.rules.state import Breach

TS = datetime(2025, 10, 6, 20, 0, tzinfo=UTC)
DAYS = [date(2025, 10, d) for d in (6, 7, 8, 9, 10)]


def res(day, outcome, *, rule=None, dtp=None, dtpay=None, payout=None, micro=0):
    return AttemptResult(
        start=day,
        outcome=outcome,
        breach=Breach(rule, "reason", TS) if rule else None,
        days_to_pass=dtp,
        days_to_payout=dtpay,
        first_payout=payout,
        microscalp_flags=micro,
        fills=(),
        equity=((TS, 500.0),),
    )


RESULTS = [
    res(DAYS[0], "payout", dtp=3, dtpay=10, payout=1000.0, micro=1),
    res(DAYS[1], "payout", dtp=5, dtpay=14, payout=2000.0),
    res(DAYS[2], "breached", rule="max_drawdown", dtp=4),  # breached while funded
    res(DAYS[3], "breached", rule="daily_loss_limit"),
    res(DAYS[4], "no_result"),
]


def test_period_result():
    rules = make_rules(eval_fee=100.0, activation_fee=50.0)
    p = period_result(RESULTS, rules, DAYS[0], DAYS[-1])
    assert p["start"] == "2025-10-06" and p["end"] == "2025-10-10"
    assert p["attempts"] == 5
    assert p["pass_rate"] == pytest.approx(0.6)
    assert p["days_to_pass"] == {"median": 4, "histogram": [[3, 1], [4, 1], [5, 1]]}
    assert p["failure_reasons"] == {"daily_loss_limit": 1, "max_drawdown": 1, "no_result": 1}
    assert p["payout_rate"] == pytest.approx(2 / 3)
    assert p["median_days_to_payout"] == 12
    assert p["median_first_payout"] == 1500.0
    assert p["p_pass_and_payout"] == pytest.approx(0.4)
    # (850 + 1850 - 150 - 100 - 100) / 5
    assert p["expected_net_per_attempt"] == pytest.approx(470.0)
    assert p["microscalp_flags"] == 1
    assert [(s["start"], s["outcome"]) for s in p["sample_equity"]] == [
        ("2025-10-06", "payout"),
        ("2025-10-07", "payout"),
        ("2025-10-08", "breached"),
        ("2025-10-09", "breached"),
    ]
    assert p["sample_equity"][0]["points"] == [["2025-10-06T20:00:00Z", 500.0]]


def test_empty_medians_are_null():
    p = period_result([res(DAYS[0], "no_result")], make_rules(), DAYS[0], DAYS[0])
    assert p["days_to_pass"] == {"median": None, "histogram": []}
    assert (p["median_days_to_payout"], p["median_first_payout"]) == (None, None)
    assert p["payout_rate"] == 0.0
    assert p["sample_equity"] == []


def period(p_pass_and_payout: float):
    return {**period_result(RESULTS, make_rules(), DAYS[0], DAYS[-1]), "p_pass_and_payout": p_pass_and_payout}


def report(tuning, holdout, include_holdout=True):
    return build_report(
        strategy=scripted({}),
        account_id="lucid-test-50k",
        rules_hash="r" * 64,
        contract="MNQ",
        data_version="d" * 64,
        holdout_months=12,
        include_holdout=include_holdout,
        tuning=tuning,
        holdout=holdout,
        created_at=TS,
    )


def test_build_report():
    r = report(period(0.4), period(0.1))
    assert r["key"] == report_key("0" * 64, "r" * 64, "d" * 64, "MNQ", 12, True)
    assert r["created_at"] == "2025-10-06T20:00:00Z"
    assert (r["strategy"], r["strategy_hash"], r["params"]) == ("scripted", "0" * 64, {})
    assert (r["account"], r["rules_hash"], r["contract"]) == ("lucid-test-50k", "r" * 64, "MNQ")
    assert (r["data_version"], r["holdout_months"]) == ("d" * 64, 12)
    assert r["holdout_gap"] is True  # 0.1 < 50% of 0.4
    assert r["note"] == NOTE


def test_holdout_gap_flag():
    assert report(period(0.4), period(0.3))["holdout_gap"] is False
    assert report(period(0.0), period(0.0))["holdout_gap"] is False
    r = report(period(0.4), None, include_holdout=False)
    assert r["periods"]["holdout"] is None and r["holdout_gap"] is False


def test_report_key_changes_with_every_input():
    base = ("s", "r", "d", "MNQ", 12, True)
    keys = {report_key(*base)}
    for i, changed in enumerate(["s2", "r2", "d2", "NQ", 6, False]):
        keys.add(report_key(*base[:i], changed, *base[i + 1 :]))
    assert len(keys) == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_report.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest.report'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/backtest/report.py`:

```python
"""Attempt results → report JSON (index: Plans 09/10). Pure."""

import hashlib
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from statistics import mean, median
from typing import Any, TypedDict

from fenrir.backtest.attempt import AttemptResult
from fenrir.rules.schema import AccountRules
from fenrir.strategy import Strategy

NOTE = (
    "Attempts start on every trading day and overlap, "
    "so these rates are estimates, not exact probabilities."
)
SAMPLES_PER_OUTCOME = 2


class DaysToPass(TypedDict):
    median: float | None
    histogram: list[list[int]]


class EquitySample(TypedDict):
    start: str
    outcome: str
    points: list[list[Any]]  # [[iso ts, equity], ...]


class PeriodResult(TypedDict):
    start: str
    end: str
    attempts: int
    pass_rate: float
    days_to_pass: DaysToPass
    failure_reasons: dict[str, int]
    payout_rate: float
    median_days_to_payout: float | None
    median_first_payout: float | None
    p_pass_and_payout: float
    expected_net_per_attempt: float
    microscalp_flags: int
    sample_equity: list[EquitySample]


class Report(TypedDict):
    key: str
    created_at: str
    strategy: str
    strategy_hash: str
    params: dict[str, Any]
    account: str
    rules_hash: str
    contract: str
    data_version: str
    holdout_months: int
    periods: dict[str, PeriodResult | None]  # "tuning", "holdout"
    holdout_gap: bool
    note: str


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values: list) -> float | None:
    return median(values) if values else None


def _samples(results: Sequence[AttemptResult]) -> list[EquitySample]:
    # shortcut: equity points are end-of-day, so a breach day's intraday drop is not plotted
    picks = [
        r
        for outcome in ("payout", "passed", "breached")
        for r in [r for r in results if r.outcome == outcome][:SAMPLES_PER_OUTCOME]
    ]
    return [
        {
            "start": r.start.isoformat(),
            "outcome": r.outcome,
            "points": [[_iso(ts), eq] for ts, eq in r.equity],
        }
        for r in sorted(picks, key=lambda r: r.start)
    ]


def period_result(
    results: Sequence[AttemptResult], rules: AccountRules, start: date, end: date
) -> PeriodResult:
    n = len(results)
    passed = [r for r in results if r.days_to_pass is not None]
    paid = [r for r in results if r.outcome == "payout"]
    costs = rules.costs
    net = [
        (r.first_payout or 0.0)
        - costs.eval_fee
        - (costs.activation_fee if r.days_to_pass is not None else 0.0)
        for r in results
    ]
    failures = Counter(r.breach.rule if r.breach else "no_result" for r in results if r.outcome != "payout")
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attempts": n,
        "pass_rate": len(passed) / n,
        "days_to_pass": {
            "median": _median([r.days_to_pass for r in passed]),
            "histogram": [[d, c] for d, c in sorted(Counter(r.days_to_pass for r in passed).items())],
        },
        "failure_reasons": dict(sorted(failures.items())),
        "payout_rate": len(paid) / len(passed) if passed else 0.0,
        "median_days_to_payout": _median([r.days_to_payout for r in paid]),
        "median_first_payout": _median([r.first_payout for r in paid]),
        "p_pass_and_payout": len(paid) / n,
        "expected_net_per_attempt": mean(net),
        "microscalp_flags": sum(r.microscalp_flags for r in results),
        "sample_equity": _samples(results),
    }


def report_key(
    strategy_hash: str,
    rules_hash: str,
    data_version: str,
    contract: str,
    holdout_months: int,
    include_holdout: bool,
) -> str:
    parts = [strategy_hash, rules_hash, data_version, contract, str(holdout_months), str(include_holdout)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def build_report(
    *,
    strategy: Strategy,
    account_id: str,
    rules_hash: str,
    contract: str,
    data_version: str,
    holdout_months: int,
    include_holdout: bool,
    tuning: PeriodResult,
    holdout: PeriodResult | None,
    created_at: datetime,
) -> Report:
    gap = holdout is not None and holdout["p_pass_and_payout"] < 0.5 * tuning["p_pass_and_payout"]
    return {
        "key": report_key(strategy.hash, rules_hash, data_version, contract, holdout_months, include_holdout),
        "created_at": _iso(created_at),
        "strategy": strategy.name,
        "strategy_hash": strategy.hash,
        "params": dict(strategy.params),
        "account": account_id,
        "rules_hash": rules_hash,
        "contract": contract,
        "data_version": data_version,
        "holdout_months": holdout_months,
        "periods": {"tuning": tuning, "holdout": holdout},
        "holdout_gap": gap,
        "note": NOTE,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_report.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/backtest/report.py tests/backtest/test_report.py
git commit -m "$(cat <<'EOF'
feat(backtest): build the report JSON from attempt results

Pass rate, failure reasons, payout rate, P(pass and payout), expected
net per attempt after fees, microscalp flags, equity samples and the
hold-out gap flag.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `run_backtest` (parallel shell)

**Files:**
- Create: `src/fenrir/backtest/run.py`
- Test: `tests/backtest/test_run.py`

**Interfaces:**
- Consumes:
  - Tasks 1–2; `run_attempt` (plan 08).
  - Plan 01: `Settings` (`data_dir`, `rules_dir`, `holdout_months`), `load_instruments`, `spec`, `front_month` (tests), `Bar`.
  - Plan 03: `load_bars(data_dir, instruments, series)`, `data_version(data_dir)`, and (tests only) `write_bars(data_dir, series, contract, bars)` — see note below.
  - Plan 05: `load_account(account_id, rules_dir)`, `rules_path(account_id, rules_dir)`.
  - Plan 07: `fenrir.strategy.loader.load_strategy`, `fenrir.strategy.loader.file_hash`.
- Produces: `fenrir.backtest.run.run_backtest(strategy_path: Path, account_id: str, contract: str, settings: Settings, *, include_holdout: bool, on_progress: Callable[[int, int], None] | None = None) -> Report` — `on_progress(done_attempts, total_attempts)` after each finished chunk. Raises `ValueError` for a contract whose series differs from the strategy's `MARKET`, unknown account/contract, or a hold-out covering all data.

Note: `fenrir.data.store.write_bars(data_dir: Path, series: str, contract: str, bars: Sequence[Bar]) -> None` is plan 03's writer (listed in the index): it writes `data/bars/<series>/1m/<contract>.parquet` and updates `manifest.json`.

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_run.py`:

```python
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from backtest_helpers import et, rules_data

from fenrir.backtest.attempts import periods
from fenrir.backtest.run import run_backtest
from fenrir.core.bars import Bar
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import load_bars, write_bars
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash

ORB = Path(__file__).parents[2] / "strategies" / "orb.py"
ACCOUNT = "lucid-test-50k"
MINUTE = timedelta(minutes=1)


def weekdays(start: date, end: date):
    return [start + timedelta(n) for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() < 5]


def day_bars(day: date, k: int, contract: str) -> list[Bar]:
    """40 one-minute bars from 09:30 ET with an upward zigzag, so ORB trades some days."""
    out, price = [], 20000.0 + 10 * k
    for m in range(40):
        c = price + 0.5 * m + (1.0 if m % 2 else -1.0) * (k % 3)
        out.append(Bar(et(day, "09:30") + m * MINUTE, price, max(price, c) + 0.5, min(price, c) - 0.5, c, 10, contract))
        price = c
    return out


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = replace(Settings(), data_dir=tmp_path / "data", rules_dir=tmp_path / "rules", holdout_months=1)
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
    run_backtest(ORB, ACCOUNT, "MNQ", settings, include_holdout=False, on_progress=lambda d, t: calls.append((d, t)))
    total = calls[-1][1]
    assert calls[-1] == (total, total)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)


def test_contract_must_match_strategy_market(settings):
    with pytest.raises(ValueError, match="MES trades ES prices but orb is an NQ strategy"):
        run_backtest(ORB, ACCOUNT, "MES", settings, include_holdout=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_run.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest.run'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/backtest/run.py`:

```python
"""Run a backtest: load inputs, fan attempts out over CPU cores, build the report. Shell."""

from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from fenrir.backtest.attempt import AttemptResult, run_attempt
from fenrir.backtest.attempts import Period, periods
from fenrir.backtest.report import Report, build_report, period_result
from fenrir.core.instruments import load_instruments, spec
from fenrir.data.store import data_version, load_bars
from fenrir.rules.loader import load_account, rules_path
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash, load_strategy

CHUNK = 16  # attempts per task: amortizes pickling without starving cores

_worker_inputs: tuple | None = None  # set once per worker process by _init_worker


def _load(strategy_path: Path, account_id: str, contract: str, settings: Settings) -> tuple:
    strategy = load_strategy(strategy_path)
    instruments = load_instruments()
    contract_spec = spec(instruments, contract)
    if contract_spec.series != strategy.market:
        raise ValueError(
            f"{contract} trades {contract_spec.series} prices but {strategy.name} "
            f"is an {strategy.market} strategy"
        )
    rules = load_account(account_id, settings.rules_dir)
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
    load_args = (strategy_path, account_id, contract, settings)
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
        tuning=summarize(tuning),
        holdout=summarize(holdout) if len(run_periods) == 2 else None,
        created_at=datetime.now(UTC),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_run.py -q`
Expected: `5 passed` (spawns worker processes; takes a few seconds)

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/backtest/run.py tests/backtest/test_run.py
git commit -m "$(cat <<'EOF'
feat(backtest): run attempts in parallel and build the report

Each worker loads its own inputs once; results are keyed by bar range
so completion order never changes the report.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `fenrir backtest` and `fenrir evaluate`

**Files:**
- Create: `src/fenrir/cli/backtest.py`
- Modify: `src/fenrir/cli/main.py`
- Test: `tests/cli/test_backtest_cli.py`

**Interfaces:**
- Consumes: `run_backtest`, `Report` (Tasks 2–3); `load_settings` (plan 01); `app` in `fenrir.cli.main` (plan 01).
- Produces:
  - `fenrir backtest STRATEGY... --account ID [--account ID ...] --contract SYMBOL [--json]` — tuning only.
  - `fenrir evaluate` — same arguments, tuning + hold-out.
  - `fenrir.cli.backtest.format_summary(report: Report) -> str`, `backtest(...)`, `evaluate(...)`, and `_run(strategies, accounts, contract, as_json, include_holdout)`. Plan 10 replaces `_run`'s body with grid expansion and the cache, and makes `--contract` optional with globs.
  - These are top-level commands, registered with `app.command("backtest")(backtest.backtest)` rather than `add_typer` (the index's group pattern would make them `fenrir backtest backtest`).

- [ ] **Step 1: Write the failing test**

`tests/cli/test_backtest_cli.py`:

```python
import json

from typer.testing import CliRunner

from fenrir.cli import backtest as cli_backtest
from fenrir.cli.main import app

PERIOD = {"pass_rate": 0.31, "p_pass_and_payout": 0.19, "expected_net_per_attempt": 87.5}
REPORT = {
    "strategy": "orb",
    "account": "lucid-flex-50k",
    "contract": "MNQ",
    "periods": {"tuning": PERIOD, "holdout": None},
    "holdout_gap": False,
}


def test_format_summary_tuning_only():
    assert cli_backtest.format_summary(REPORT) == (
        "orb  lucid-flex-50k  MNQ  tuning: pass 31.0% · pass+payout 19.0% · net $87.50/attempt"
    )


def test_format_summary_with_holdout_gap():
    holdout = {"pass_rate": 0.2, "p_pass_and_payout": 0.05, "expected_net_per_attempt": -40.0}
    report = {**REPORT, "periods": {"tuning": PERIOD, "holdout": holdout}, "holdout_gap": True}
    assert cli_backtest.format_summary(report).endswith(
        "hold-out: pass 20.0% · pass+payout 5.0% · net $-40.00/attempt  HOLD-OUT GAP"
    )


def fake_runs(monkeypatch):
    calls = []

    def fake(path, account, contract, settings, *, include_holdout, on_progress=None):
        calls.append((str(path), account, contract, include_holdout))
        return REPORT

    monkeypatch.setattr(cli_backtest, "run_backtest", fake)
    return calls


def test_backtest_is_tuning_only(monkeypatch):
    calls = fake_runs(monkeypatch)
    args = ["backtest", "a.py", "b.py", "--account", "lucid-flex-50k", "--account", "lucid-pro-50k", "--contract", "MNQ"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls == [
        ("a.py", "lucid-flex-50k", "MNQ", False),
        ("a.py", "lucid-pro-50k", "MNQ", False),
        ("b.py", "lucid-flex-50k", "MNQ", False),
        ("b.py", "lucid-pro-50k", "MNQ", False),
    ]
    assert result.output.count("tuning: pass 31.0%") == 4


def test_evaluate_includes_holdout_and_json(monkeypatch):
    calls = fake_runs(monkeypatch)
    result = CliRunner().invoke(app, ["evaluate", "a.py", "--account", "lucid-flex-50k", "--contract", "MNQ", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == [("a.py", "lucid-flex-50k", "MNQ", True)]
    assert json.loads(result.output) == [REPORT]


def test_errors_exit_1_with_message(monkeypatch):
    def boom(*a, **k):
        raise ValueError("MES trades ES prices but orb is an NQ strategy")

    monkeypatch.setattr(cli_backtest, "run_backtest", boom)
    result = CliRunner().invoke(app, ["backtest", "a.py", "--account", "x", "--contract", "MES"])
    assert result.exit_code == 1
    assert "error: MES trades ES prices" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_backtest_cli.py -q`
Expected: FAIL with `ImportError: cannot import name 'backtest' from 'fenrir.cli'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/cli/backtest.py`:

```python
"""`fenrir backtest` (tuning only) and `fenrir evaluate` (tuning + hold-out). Thin shell."""

import json
from pathlib import Path
from typing import Annotated

import typer

from fenrir.backtest.report import PeriodResult, Report
from fenrir.backtest.run import run_backtest
from fenrir.settings import load_settings

Strategies = Annotated[list[Path], typer.Argument(help="Strategy files")]
Accounts = Annotated[list[str], typer.Option("--account", help="Rules account id, e.g. lucid-flex-50k")]
Contract = Annotated[str, typer.Option("--contract", help="ES, MES, NQ or MNQ")]
AsJson = Annotated[bool, typer.Option("--json", help="Print report JSON")]


def _period(p: PeriodResult) -> str:
    return (
        f"pass {p['pass_rate']:.1%} · pass+payout {p['p_pass_and_payout']:.1%} · "
        f"net ${p['expected_net_per_attempt']:,.2f}/attempt"
    )


def format_summary(report: Report) -> str:
    line = f"{report['strategy']}  {report['account']}  {report['contract']}  tuning: {_period(report['periods']['tuning'])}"
    holdout = report["periods"]["holdout"]
    if holdout is not None:
        line += f"  hold-out: {_period(holdout)}"
    if report["holdout_gap"]:
        line += "  HOLD-OUT GAP"
    return line


def _run(strategies: list[Path], accounts: list[str], contract: str, as_json: bool, include_holdout: bool) -> None:
    settings = load_settings()
    try:
        reports = [
            run_backtest(path, account, contract, settings, include_holdout=include_holdout)
            for path in strategies
            for account in accounts
        ]
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(json.dumps(reports, indent=2) if as_json else "\n".join(map(format_summary, reports)))


def backtest(strategies: Strategies, account: Accounts, contract: Contract, as_json: AsJson = False) -> None:
    """Backtest on the tuning period only (never sees the hold-out)."""
    _run(strategies, account, contract, as_json, include_holdout=False)


def evaluate(strategies: Strategies, account: Accounts, contract: Contract, as_json: AsJson = False) -> None:
    """Backtest on the tuning and hold-out periods, side by side."""
    _run(strategies, account, contract, as_json, include_holdout=True)
```

In `src/fenrir/cli/main.py`, add after the existing `app = typer.Typer(...)` line and any `app.add_typer(...)` lines:

```python
from fenrir.cli import backtest  # noqa: E402

app.command("backtest")(backtest.backtest)
app.command("evaluate")(backtest.evaluate)
```

(If `main.py` keeps all imports at the top, move the import there instead and drop the `noqa`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cli/test_backtest_cli.py -q`
Expected: `5 passed`. If `test_errors_exit_1_with_message` fails because `result.output` lacks stderr, construct the runner as `CliRunner(mix_stderr=True)` for older Click, or assert on `result.stderr` for Click ≥ 8.2 — keep whichever matches the installed Click.

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass (run `uv run ruff format .` and re-run if formatting differs).

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/cli/backtest.py src/fenrir/cli/main.py tests/cli/test_backtest_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): add fenrir backtest and fenrir evaluate

backtest runs the tuning period only; evaluate adds the hold-out.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Measure performance (spec §6 Performance)

**Files:**
- Modify: this plan file — fill in **Done notes** at the bottom.

**Interfaces:**
- Consumes: `fenrir evaluate` (Task 4), `run_attempt` (plan 08), real history from `fenrir data pull` (plan 03), `rules/lucid/flex-50k.yaml` (plan 05).
- Produces: recorded measurements and a go / follow-up decision.

**Threshold:** one `fenrir evaluate strategies/orb.py --account lucid-flex-50k --contract MNQ` over **all pulled history** (tuning + hold-out) finishes in **≤ 60 s wall time** on the development machine. Why 60 s: a realistic grid (3 strategies × ~10 Lucid account types × 2 contracts = 60 runs) then finishes within an hour, cached afterwards (plan 10).

- [ ] **Step 1: Confirm the inputs exist**

Run: `uv run fenrir data summary && ls rules/lucid/flex-50k.yaml strategies/orb.py`
Expected: NQ coverage spanning years; both files listed. If NQ history is missing, run `uv run fenrir data pull --series NQ` first.

- [ ] **Step 2: Time a full evaluate**

macOS: `/usr/bin/time -l uv run fenrir evaluate strategies/orb.py --account lucid-flex-50k --contract MNQ`
Linux/Docker: `/usr/bin/time -v uv run fenrir evaluate strategies/orb.py --account lucid-flex-50k --contract MNQ`
Record: wall time ("real" / "Elapsed"), peak RSS of the parent ("maximum resident set size"), CPU count (`python3 -c "import os; print(os.cpu_count())"`), number of attempts (tuning + hold-out `attempts` from `--json`), first and last data day.

- [ ] **Step 3: Profile one worker's share in-process**

```bash
uv run python - <<'EOF'
import cProfile, pstats
from pathlib import Path
from fenrir.backtest.attempts import periods
from fenrir.backtest.run import _load
from fenrir.backtest.attempt import run_attempt
from fenrir.settings import load_settings

s = load_settings()
bars, strategy, rules, spec = _load(Path("strategies/orb.py"), "lucid-flex-50k", "MNQ", s)
tuning, _ = periods(bars, s.holdout_months)
sample = tuning.ranges[:: max(1, len(tuning.ranges) // 40)]
prof = cProfile.Profile()
prof.enable()
for a, b in sample:
    run_attempt(bars, a, b, strategy, rules, spec)
prof.disable()
pstats.Stats(prof).sort_stats("cumulative").print_stats(15)
EOF
```

Record the top 5 functions by cumulative time and the per-attempt average.

- [ ] **Step 4: Decide**

- **≤ 60 s:** done. Write the numbers in Done notes.
- **> 60 s:** do **not** optimize in this plan. Write the numbers and the profile in Done notes and add a follow-up plan `docs/superpowers/plans/<date>-NN-backtest-speed.md` that tries, in this order, stopping at the first that gets under 60 s: (1) give `Ctx.bars` a read-only window over the shared tuple instead of copying up to 500 bars every step (needs an index contract change: `bars: Sequence[Bar]`); (2) precompute indicators in bulk once per series (polars) and expose them to `step` through `Ctx`; (3) compile the fold (`attempt.py`, `fills.py`, rule functions) with mypyc. The functional design stays in every option (spec §6).

- [ ] **Step 5: Commit the notes**

```bash
git add docs/superpowers/plans/2026-10-10-09-attempts-report.md
git commit -m "$(cat <<'EOF'
docs(backtest): record backtest performance measurement

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

## Done notes

(Filled in by Task 5.)

- Machine / CPUs:
- Data range / attempts (tuning + hold-out):
- Wall time / parent peak RSS:
- Top cumulative functions:
- Decision:
