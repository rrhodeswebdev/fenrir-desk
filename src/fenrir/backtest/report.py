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
    expected_gross_per_attempt: float
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
    dll_on: bool
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
    results: Sequence[AttemptResult],
    rules: AccountRules,
    start: date,
    end: date,
) -> PeriodResult:
    n = len(results)
    passed = [r for r in results if r.days_to_pass is not None]
    paid = [r for r in results if r.outcome == "payout"]
    costs = rules.costs
    gross = [
        (r.first_payout or 0.0) - (costs.activation_fee if r.days_to_pass is not None else 0.0)
        for r in results
    ]
    failures = Counter(
        r.breach.rule if r.breach else "no_result" for r in results if r.outcome != "payout"
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "attempts": n,
        "pass_rate": len(passed) / n,
        "days_to_pass": {
            "median": _median([r.days_to_pass for r in passed]),
            "histogram": [
                [d, c] for d, c in sorted(Counter(r.days_to_pass for r in passed).items())
            ],
        },
        "failure_reasons": dict(sorted(failures.items())),
        "payout_rate": len(paid) / len(passed) if passed else 0.0,
        "median_days_to_payout": _median([r.days_to_payout for r in paid]),
        "median_first_payout": _median([r.first_payout for r in paid]),
        "p_pass_and_payout": len(paid) / n,
        "expected_gross_per_attempt": mean(gross),
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
    dll_on: bool,
) -> str:
    parts = [
        strategy_hash,
        rules_hash,
        data_version,
        contract,
        str(holdout_months),
        str(include_holdout),
        str(dll_on),
    ]
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
    dll_on: bool,
    tuning: PeriodResult,
    holdout: PeriodResult | None,
    created_at: datetime,
) -> Report:
    gap = holdout is not None and holdout["p_pass_and_payout"] < 0.5 * tuning["p_pass_and_payout"]
    return {
        "key": report_key(
            strategy.hash,
            rules_hash,
            data_version,
            contract,
            holdout_months,
            include_holdout,
            dll_on,
        ),
        "created_at": _iso(created_at),
        "strategy": strategy.name,
        "strategy_hash": strategy.hash,
        "params": dict(strategy.params),
        "account": account_id,
        "rules_hash": rules_hash,
        "contract": contract,
        "data_version": data_version,
        "holdout_months": holdout_months,
        "dll_on": dll_on,
        "periods": {"tuning": tuning, "holdout": holdout},
        "holdout_gap": gap,
        "note": NOTE,
    }
