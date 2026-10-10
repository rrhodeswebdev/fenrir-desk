import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

_path = Path(__file__).parents[2] / "scripts" / "lse_gate.py"
_spec = importlib.util.spec_from_file_location("lse_gate", _path)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_weekday_gaps_collapses_runs_and_skips_weekends():
    # Mon 2026-10-05 .. Fri 2026-10-16; missing Thu 8, Fri 9, Mon 12 (weekend between) and Wed 14
    present = {date(2026, 10, d) for d in (5, 6, 7, 13, 15, 16)}
    assert gate.weekday_gaps(present, date(2026, 10, 5), date(2026, 10, 16)) == [
        (date(2026, 10, 8), date(2026, 10, 12)),
        (date(2026, 10, 14), date(2026, 10, 14)),
    ]


def test_weekday_gaps_none():
    present = {date(2026, 10, 5), date(2026, 10, 6)}
    assert gate.weekday_gaps(present, date(2026, 10, 5), date(2026, 10, 6)) == []


def test_bucket_ticks_orders_by_time():
    ticks = [
        {"ts": utc(2026, 10, 8, 14, 30, 50), "price": 103.0, "size": 1, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 30, 1), "price": 100.0, "size": 2, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 30, 20), "price": 99.0, "size": 1, "symbol": "NQZ6"},
        {"ts": utc(2026, 10, 8, 14, 31, 0), "price": 104.0, "size": 5, "symbol": "NQZ6"},
    ]
    assert gate.bucket_ticks(ticks) == {
        ("NQZ6", utc(2026, 10, 8, 14, 30)): (100.0, 103.0, 99.0, 103.0, 4),
        ("NQZ6", utc(2026, 10, 8, 14, 31)): (104.0, 104.0, 104.0, 104.0, 5),
    }


def test_compare_bars_within_one_tick_matches():
    key = ("NQZ6", utc(2026, 10, 8, 14, 30))
    live = {key: (100.0, 103.0, 99.0, 103.0, 4)}
    hist = {key: (100.25, 103.0, 99.0, 102.75, 9)}
    assert gate.compare_bars(live, hist, tick=0.25) == (1, [])


def test_compare_bars_reports_mismatch():
    key = ("NQZ6", utc(2026, 10, 8, 14, 30))
    live = {key: (100.0, 103.0, 99.0, 103.0, 4)}
    hist = {key: (100.0, 104.0, 99.0, 103.0, 4)}
    compared, problems = gate.compare_bars(live, hist, tick=0.25)
    assert compared == 1
    assert problems == ["NQZ6 2026-10-08T14:30:00+00:00 high live=103.0 hist=104.0"]


def test_compare_bars_no_overlap_is_not_a_pass():
    live = {("NQZ6", utc(2026, 10, 8, 14, 30)): (1.0, 1.0, 1.0, 1.0, 1)}
    hist = {("NQZ6", utc(2026, 10, 8, 18, 30)): (1.0, 1.0, 1.0, 1.0, 1)}
    compared, problems = gate.compare_bars(live, hist, tick=0.25)
    assert compared == 0
    assert problems == [
        "no overlapping minutes: check symbol, timezone and bar-open/close convention"
    ]


def test_lag_stats():
    assert gate.lag_stats([0.25, 0.75, 0.5, 9.0]) == (0.625, 9.0)
