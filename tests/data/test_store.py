import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from fenrir.core.bars import Bar
from fenrir.core.instruments import parse_instruments
from fenrir.core.sessions import trading_day
from fenrir.data.pull import Window
from fenrir.data.store import (
    _atomic_write,
    coverage,
    data_version,
    find_gaps,
    load_bars,
    read_manifest,
    trading_day_expr,
    with_contract,
    write_bars,
    write_contract,
    write_manifest,
)

INST = parse_instruments(
    {
        "contracts": [
            {"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1}
        ],
        "rollover": {"series": ["NQ"], "first_year": 2025, "last_year": 2026},
    }
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def frame(rows: list[tuple[datetime, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [ts for ts, _ in rows],
            "open": [p for _, p in rows],
            "high": [p for _, p in rows],
            "low": [p for _, p in rows],
            "close": [p for _, p in rows],
            "volume": [1 for _ in rows],
        }
    )


def roll_fixture(data_dir):
    # NQ rolls Z25 -> H26 on trading day 2025-12-11; trading days 12-11..12-16 are the roll window.
    # 23:30 UTC is 18:30 EST, so 12-10 23:30 is trading day 12-11 and 12-16 23:30 is 12-17.
    times = [
        utc(2025, 12, 10, 14, 30),
        utc(2025, 12, 10, 23, 30),
        utc(2025, 12, 11, 14, 30),
        utc(2025, 12, 16, 23, 30),
        utc(2025, 12, 17, 14, 30),
    ]
    write_contract(
        data_dir,
        "NQ",
        "NQZ25",
        frame(list(zip(times, [1.0, 2.0, 3.0, 4.0, 5.0], strict=True))),
    )
    write_contract(
        data_dir,
        "NQ",
        "NQH26",
        frame(list(zip(times, [11.0, 12.0, 13.0, 14.0, 15.0], strict=True))),
    )


def bar(ts: datetime, p: float, contract: str) -> Bar:
    return Bar(ts, p, p, p, p, 1, contract)


def test_load_bars_uses_front_month_per_trading_day(tmp_path):
    roll_fixture(tmp_path)
    assert load_bars(tmp_path, INST, "NQ") == (
        bar(utc(2025, 12, 10, 14, 30), 1.0, "NQZ25"),
        bar(utc(2025, 12, 16, 23, 30), 14.0, "NQH26"),
        bar(utc(2025, 12, 17, 14, 30), 15.0, "NQH26"),
    )


def test_load_bars_filters_by_trading_day(tmp_path):
    roll_fixture(tmp_path)
    day = date(2025, 12, 17)
    assert [b.close for b in load_bars(tmp_path, INST, "NQ", day, day)] == [14.0, 15.0]
    day = date(2025, 12, 10)
    assert [b.close for b in load_bars(tmp_path, INST, "NQ", day, day)] == [1.0]
    day = date(2025, 12, 11)  # roll window
    assert load_bars(tmp_path, INST, "NQ", day, day) == ()


def test_load_bars_empty_dir(tmp_path):
    assert load_bars(tmp_path, INST, "NQ") == ()


def test_write_contract_dedupes_and_sorts(tmp_path):
    rows = [
        (utc(2025, 12, 1, 14, 31), 2.0),
        (utc(2025, 12, 1, 14, 30), 1.0),
        (utc(2025, 12, 1, 14, 31), 9.0),
    ]
    assert write_contract(tmp_path, "NQ", "NQZ25", frame(rows)) == 2
    assert [b.close for b in load_bars(tmp_path, INST, "NQ")] == [1.0, 9.0]


def test_trading_day_expr_matches_core():
    times = [
        utc(2026, 3, 8, 22, 30),
        utc(2026, 11, 1, 22, 30),
        utc(2026, 11, 1, 23, 0),
        utc(2026, 10, 8, 21, 59),
        utc(2026, 10, 8, 22, 0),
    ]
    got = pl.DataFrame({"ts": times}).select(trading_day_expr()).to_series().to_list()
    assert got == [trading_day(t) for t in times]


def test_find_gaps():
    expected = [date(2025, 11, d) for d in (24, 25, 26, 28)] + [
        date(2025, 12, 1),
        date(2025, 12, 2),
    ]
    present = {date(2025, 11, 24), date(2025, 11, 28), date(2025, 12, 2)}
    assert find_gaps(present, expected) == (
        (date(2025, 11, 25), date(2025, 11, 26)),
        (date(2025, 12, 1), date(2025, 12, 1)),
    )


def test_coverage_skips_cme_holidays(tmp_path):
    # Christmas 2025-12-25 is a full CME close; Christmas Eve is a shortened session, so it counts
    days = [(2025, 12, 22), (2025, 12, 23), (2025, 12, 26), (2025, 12, 29)]
    write_contract(tmp_path, "NQ", "NQZ25", frame([(utc(*d, 15, 0), 1.0) for d in days]))
    assert coverage(tmp_path, "NQ").gaps == ((date(2025, 12, 24), date(2025, 12, 24)),)
    c = coverage(tmp_path, "NQ")
    assert (c.first_day, c.last_day, c.days) == (date(2025, 12, 22), date(2025, 12, 29), 4)


def test_coverage_skips_roll_window(tmp_path):
    days = [(2025, 12, 10), (2025, 12, 11), (2025, 12, 17)]
    write_contract(tmp_path, "NQ", "NQZ25", frame([(utc(*d, 15, 0), 1.0) for d in days]))
    c = coverage(tmp_path, "NQ")
    assert (c.first_day, c.last_day, c.days, c.gaps) == (
        date(2025, 12, 10),
        date(2025, 12, 17),
        2,
        (),
    )


def test_coverage_empty(tmp_path):
    c = coverage(tmp_path, "ES")
    assert (c.first_day, c.last_day, c.days, c.gaps) == (None, None, 0, ())


def test_manifest_and_data_version(tmp_path):
    assert data_version(tmp_path) == hashlib.sha256(b"").hexdigest()
    assert read_manifest(tmp_path) == {"series": {}}
    w = Window("NQ", "NQZ25", date(2025, 9, 11), date(2025, 12, 10))
    m = with_contract(
        {"series": {}},
        w,
        rows=5,
        today=date(2025, 12, 20),
        pulled_at="2025-12-20T00:00:00Z",
    )
    write_manifest(tmp_path, m)
    assert read_manifest(tmp_path) == m
    assert m["series"]["NQ"]["NQZ25"] == {
        "first_day": "2025-09-11",
        "last_day": "2025-12-10",
        "rows": 5,
        "complete": True,
    }
    first = data_version(tmp_path)
    write_manifest(tmp_path, {**m, "pulled_at": "2025-12-21T00:00:00Z"})
    assert data_version(tmp_path) != first
    assert (
        json.loads((tmp_path / "bars" / "manifest.json").read_text())["pulled_at"]
        == "2025-12-21T00:00:00Z"
    )


def test_write_bars_merges_and_refreshes_manifest(tmp_path):
    a = bar(utc(2025, 12, 1, 14, 30), 1.0, "NQZ25")
    b = bar(utc(2025, 12, 1, 14, 31), 2.0, "NQZ25")
    write_bars(tmp_path, "NQ", "NQZ25", [a, b])
    first = data_version(tmp_path)
    b2 = bar(utc(2025, 12, 1, 14, 31), 5.0, "NQZ25")
    c = bar(utc(2025, 12, 2, 14, 30), 3.0, "NQZ25")
    write_bars(tmp_path, "NQ", "NQZ25", [b2, c])
    assert load_bars(tmp_path, INST, "NQ") == (a, b2, c)
    entry = read_manifest(tmp_path)["series"]["NQ"]["NQZ25"]
    assert (entry["first_day"], entry["last_day"], entry["rows"]) == ("2025-12-01", "2025-12-02", 3)
    assert data_version(tmp_path) != first


def test_with_contract_current_window_is_incomplete():
    w = Window("NQ", "NQH26", date(2025, 12, 11), date(2026, 1, 5))
    m = with_contract({"series": {}}, w, rows=1, today=date(2026, 1, 5), pulled_at="x")
    assert m["series"]["NQ"]["NQH26"]["complete"] is False


@pytest.mark.parametrize("days_back, complete", [(0, False), (1, False), (2, True)])
def test_with_contract_complete_cutoff(days_back, complete):
    today = date(2026, 1, 5)
    w = Window("NQ", "NQH26", date(2025, 12, 11), today - timedelta(days=days_back))
    m = with_contract({"series": {}}, w, rows=1, today=today, pulled_at="x")
    assert m["series"]["NQ"]["NQH26"]["complete"] is complete


# data/ is terminal-writable in the image (DEC-1): no planted link may redirect a write (#135)
NQ_ROWS = [(utc(2025, 12, 1, 15, 0), 1.0)]


def test_planted_tmp_symlinks_are_never_followed(tmp_path):
    data, outside = tmp_path / "data", tmp_path / "outside"
    (data / "bars" / "NQ" / "1m").mkdir(parents=True)
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("keep")
    (data / "bars" / "manifest.json.tmp").symlink_to(secret)
    (data / "bars" / "NQ" / "1m" / "NQZ25.parquet.tmp").symlink_to(secret)
    write_contract(data, "NQ", "NQZ25", frame(NQ_ROWS))
    write_manifest(data, {"series": {}})
    assert secret.read_text() == "keep"
    assert read_manifest(data) == {"series": {}}
    assert load_bars(data, INST, "NQ") == (bar(*NQ_ROWS[0], "NQZ25"),)
    manifest = data / "bars" / "manifest.json"
    assert not manifest.is_symlink() and manifest.stat().st_mode & 0o777 == 0o644
    leftovers = {p.name for p in data.rglob("*.tmp")}
    assert leftovers == {"manifest.json.tmp", "NQZ25.parquet.tmp"}  # only the planted links


@pytest.mark.parametrize("link", ["", "bars", "bars/NQ", "bars/NQ/1m"])
def test_symlinked_directory_refuses_the_write(tmp_path, link):
    data, outside = tmp_path / "data", tmp_path / "outside"
    outside.mkdir()
    (data / link).parent.mkdir(parents=True, exist_ok=True)
    (data / link).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_contract(data, "NQ", "NQZ25", frame(NQ_ROWS))
    if link in ("", "bars"):  # on the manifest's path too
        with pytest.raises(ValueError, match="symlink"):
            write_manifest(data, {"series": {}})
    assert list(outside.rglob("*")) == []


def test_symlinked_target_refuses_the_write(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("keep")
    (tmp_path / "bars").mkdir()
    (tmp_path / "bars" / "manifest.json").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        write_manifest(tmp_path, {"series": {}})
    assert secret.read_text() == "keep"


def test_failed_write_leaves_no_temp_file(tmp_path):
    def fail(f):
        f.write(b"half")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _atomic_write(tmp_path, tmp_path / "bars" / "manifest.json", fail)
    assert list((tmp_path / "bars").iterdir()) == []
