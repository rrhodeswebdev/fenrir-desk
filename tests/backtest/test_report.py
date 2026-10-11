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
    res(DAYS[3], "breached", rule="flat_by"),
    res(DAYS[4], "no_result"),
]


def test_period_result():
    rules = make_rules(activation_fee=50.0)
    p = period_result(RESULTS, rules, DAYS[0], DAYS[-1])
    assert p["start"] == "2025-10-06" and p["end"] == "2025-10-10"
    assert p["attempts"] == 5
    assert p["pass_rate"] == pytest.approx(0.6)
    assert p["days_to_pass"] == {"median": 4, "histogram": [[3, 1], [4, 1], [5, 1]]}
    assert p["failure_reasons"] == {"flat_by": 1, "max_drawdown": 1, "no_result": 1}
    assert p["payout_rate"] == pytest.approx(2 / 3)
    assert p["median_days_to_payout"] == 12
    assert p["median_first_payout"] == 1500.0
    assert p["p_pass_and_payout"] == pytest.approx(0.4)
    # (1000 - 50 + 2000 - 50 + 0 - 50 + 0 + 0) / 5: activation fee on the
    # 3 passed, no evaluation price
    assert p["expected_gross_per_attempt"] == pytest.approx(570.0)
    assert p["microscalp_flags"] == 1
    assert [(s["start"], s["outcome"]) for s in p["sample_equity"]] == [
        ("2025-10-06", "payout"),
        ("2025-10-07", "payout"),
        ("2025-10-08", "breached"),
        ("2025-10-09", "breached"),
    ]
    assert p["sample_equity"][0]["points"] == [["2025-10-06T20:00:00Z", 500.0]]


def test_sample_equity_keeps_the_earliest_two_per_outcome():
    days = [date(2025, 10, d) for d in range(1, 10)]
    outcomes = ["breached", "passed", "payout"] * 3  # interleaved, so order is not by outcome
    extra = {"payout": {"dtp": 2, "dtpay": 5, "payout": 100.0}, "passed": {"dtp": 2}}
    results = [res(d, o, **extra.get(o, {})) for d, o in zip(days, outcomes, strict=True)]
    p = period_result(results, make_rules(), days[0], days[-1])
    assert [(s["start"], s["outcome"]) for s in p["sample_equity"]] == [
        ("2025-10-01", "breached"),
        ("2025-10-02", "passed"),
        ("2025-10-03", "payout"),
        ("2025-10-04", "breached"),
        ("2025-10-05", "passed"),
        ("2025-10-06", "payout"),
    ]


def test_empty_medians_are_null():
    p = period_result([res(DAYS[0], "no_result")], make_rules(), DAYS[0], DAYS[0])
    assert p["days_to_pass"] == {"median": None, "histogram": []}
    assert (p["median_days_to_payout"], p["median_first_payout"]) == (None, None)
    assert p["payout_rate"] == 0.0
    assert p["sample_equity"] == []


def period(p_pass_and_payout: float):
    return {
        **period_result(RESULTS, make_rules(), DAYS[0], DAYS[-1]),
        "p_pass_and_payout": p_pass_and_payout,
    }


def report(tuning, holdout, include_holdout=True):
    return build_report(
        strategy=scripted({}),
        account_id="lucid-test-50k",
        rules_hash="r" * 64,
        contract="MNQ",
        data_version="d" * 64,
        holdout_months=12,
        include_holdout=include_holdout,
        dll_on=False,
        tuning=tuning,
        holdout=holdout,
        created_at=TS,
    )


def test_build_report():
    r = report(period(0.4), period(0.1))
    assert r["key"] == report_key("0" * 64, "r" * 64, "d" * 64, "MNQ", 12, True, False)
    assert r["created_at"] == "2025-10-06T20:00:00Z"
    assert (r["strategy"], r["strategy_hash"], r["params"]) == ("scripted", "0" * 64, {})
    assert (r["account"], r["rules_hash"], r["contract"]) == ("lucid-test-50k", "r" * 64, "MNQ")
    assert (r["data_version"], r["holdout_months"], r["dll_on"]) == ("d" * 64, 12, False)
    assert r["holdout_gap"] is True  # 0.1 < 50% of 0.4
    assert r["note"] == NOTE


def test_holdout_gap_flag():
    assert report(period(0.4), period(0.3))["holdout_gap"] is False
    assert report(period(0.0), period(0.0))["holdout_gap"] is False
    r = report(period(0.4), None, include_holdout=False)
    assert r["periods"]["holdout"] is None and r["holdout_gap"] is False


def test_report_key_changes_with_every_input():
    base = ("s", "r", "d", "MNQ", 12, True, False)
    keys = {report_key(*base)}
    for i, changed in enumerate(["s2", "r2", "d2", "NQ", 6, False, True]):
        keys.add(report_key(*base[:i], changed, *base[i + 1 :]))
    assert len(keys) == 8
