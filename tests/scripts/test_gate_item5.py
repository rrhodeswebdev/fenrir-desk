import importlib.util
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fenrir.core.bars import Bar
from fenrir.core.instruments import load_instruments
from fenrir.live import journal

_root = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("gate_item5", _root / "scripts" / "gate_item5.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SPECS = {s.symbol: s for s in load_instruments(_root / "instruments.yaml").specs}
DAY = date(2026, 10, 12)  # a Monday


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def bar(m: int, low: float, high: float, contract: str = "NQZ26", h: int = 13) -> dict:
    """A `bar` payload as it reads back from the journal: the runner's asdict(Bar) through
    journal.write's json.dumps(default=str), so ts is "2026-10-12 13:31:00+00:00"."""
    b = Bar(utc(2026, 10, 12, h, m), low, high, low, high, 10, contract)
    return json.loads(json.dumps({**asdict(b), "actions": 0}, default=str))


def fill(ts: str, price: float, instrument: str = "MNQ 12-26", qty: int = 1) -> dict:
    """An add-on `fill` message as the runner journals it (contracts: WS /stream)."""
    return {
        "type": "fill",
        "account": "Sim101",
        "client_id": "c1",
        "order_id": "o1",
        "instrument": instrument,
        "qty": qty,
        "price": price,
        "commission": 0.52,
        "ts": ts,
    }


def journalled(tmp_path: Path, bars: list[dict], fills: list[dict]) -> Path:
    """A real fenrir.db written through journal.write, as the hub writes it."""
    db = tmp_path / "fenrir.db"
    conn = journal.open_db(db)
    for b in bars:
        journal.write(conn, gate.parse_ts(b["ts"]), "Sim101", "bar", b)
    for f in fills:
        journal.write(conn, gate.parse_ts(f["ts"]), "Sim101", "fill", f)
    conn.close()
    return db


def run(tmp_path: Path, bars: list[dict], fills: list[dict]):
    fs, bs = gate.load(journalled(tmp_path, bars, fills), "Sim101", [DAY])
    return gate.compare_fills(fs, gate.bars_by_minute(bs), SPECS)


def test_parse_ts_reads_the_addons_z_and_the_bar_rows_space_form_as_the_same_utc():
    z = gate.parse_ts("2026-10-12T13:31:00.000Z")
    space = gate.parse_ts("2026-10-12 13:31:00+00:00")
    assert z == space == utc(2026, 10, 12, 13, 31)
    assert z.tzinfo == space.tzinfo == UTC
    assert gate.parse_ts("2026-10-12T09:31:00-04:00") == utc(2026, 10, 12, 13, 31)


def test_parse_ts_refuses_a_naive_timestamp():
    with pytest.raises(ValueError, match="naive"):
        gate.parse_ts("2026-10-12 13:31:00")


def test_journalled_bars_and_fills_match_by_minute(tmp_path):
    # the plan 22 bug: bars keyed by str(datetime) ("2026-10-12 13:31:00+00:00"), fills by
    # "…T13:31:01.123Z", so [:16] never matched and the gate passed with nothing compared
    bars = [bar(m, 30000.0, 30010.0) for m in range(12)]
    fills = [fill(f"2026-10-12T13:{m:02d}:01.123Z", 30005.0) for m in range(12)]
    db = journalled(tmp_path, bars, fills)
    conn = journal.open_ro(db)
    raw = dict(conn.execute("SELECT kind, payload_json FROM journal ORDER BY id DESC").fetchall())
    conn.close()
    # the two formats really are in the journal: the oldest row of each kind is minute 0
    assert '"ts": "2026-10-12 13:00:00+00:00"' in raw["bar"]
    assert '"ts": "2026-10-12T13:00:01.123Z"' in raw["fill"]
    fs, bs = gate.load(db, "Sim101", [DAY])
    result = gate.compare_fills(fs, gate.bars_by_minute(bs), SPECS)
    assert result.seen == 12
    assert len(result.compared) == 12
    assert result.unmatched == ()
    assert gate.passed(result)


def test_under_ten_fills_compared_fails(tmp_path):
    bars = [bar(m, 30000.0, 30010.0) for m in range(10)]
    nine = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(9)]
    result = run(tmp_path, bars, nine)
    assert len(result.compared) == 9
    assert not gate.passed(result)
    assert gate.report(result)[-2:] == ["   9 fills compared, need 10", "FAIL"]


def test_ten_fills_compared_inside_their_bars_pass():
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0) for m in range(10)])
    ten = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    result = gate.compare_fills(ten, bars, SPECS)
    assert gate.passed(result)
    assert gate.report(result) == [
        "fills seen=10 compared=10 unmatched=0 skipped=0 worst=0.00 ticks (tolerance 1 tick)",
        "PASS",
    ]


def test_nothing_compared_fails_even_with_fills_seen():
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0, h=14) for m in range(12)])
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(12)]
    result = gate.compare_fills(fills, bars, SPECS)
    assert (result.seen, len(result.compared), len(result.unmatched)) == (12, 0, 12)
    lines = gate.report(result)
    assert lines[0] == (
        "fills seen=12 compared=0 unmatched=12 skipped=0 worst=n/a ticks (tolerance 1 tick)"
    )
    assert lines[-1] == "FAIL"


def test_a_fill_at_59_999_is_in_its_minute_and_at_00_000_in_the_next():
    bars = gate.bars_by_minute([bar(31, 100.0, 101.0), bar(32, 200.0, 201.0)])
    late, next_ = (
        fill("2026-10-12T13:31:59.999Z", 101.0),
        fill("2026-10-12T13:32:00.000Z", 200.0),
    )
    result = gate.compare_fills([late, next_], bars, SPECS)
    assert [(c.fill, c.low, c.ticks) for c in result.compared] == [
        (late, 100.0, 0.0),
        (next_, 200.0, 0.0),
    ]


def test_ticks_outside():
    assert gate.ticks_outside(100.5, 100.0, 101.0, 0.25) == 0
    assert gate.ticks_outside(100.0, 100.0, 101.0, 0.25) == 0  # on the low
    assert gate.ticks_outside(101.0, 100.0, 101.0, 0.25) == 0  # on the high
    assert gate.ticks_outside(101.5, 100.0, 101.0, 0.25) == 2  # above
    assert gate.ticks_outside(99.75, 100.0, 101.0, 0.25) == 1  # below


def test_one_tick_out_passes_and_two_fail_listed_with_the_worst():
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0) for m in range(12)])
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    fills += [fill("2026-10-12T13:10:30.000Z", 30010.25)]  # 1 tick above: within
    fills += [fill("2026-10-12T13:11:30.000Z", 29999.5, qty=-1)]  # 2 ticks below: beyond
    result = gate.compare_fills(fills, bars, SPECS)
    assert [c.ticks for c in gate.beyond(result)] == [2.0]
    assert not gate.passed(result)
    assert gate.report(result) == [
        "fills seen=12 compared=12 unmatched=0 skipped=0 worst=2.00 ticks (tolerance 1 tick)",
        "   beyond: 2026-10-12T13:11:30.000Z (+30.000s) MNQ 12-26 qty=-1 price=29999.5"
        " bar low=30000.0 high=30010.0 -> 2.00 ticks",
        "FAIL",
    ]


def test_float_noise_on_the_bar_is_not_a_tick_beyond():
    bars = gate.bars_by_minute([bar(m, 7820.0, 7827.249999999999, "ESZ26") for m in range(10)])
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 7827.5, "MES 12-26") for m in range(10)]
    result = gate.compare_fills(fills, bars, SPECS)
    assert gate.passed(result)


def test_a_fill_with_no_bar_is_unmatched_and_listed(tmp_path):
    bars = [bar(m, 30000.0, 30010.0) for m in range(10)]
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    gap = fill("2026-10-12T13:45:10.500Z", 30005.0)  # e.g. the server was down that minute
    result = run(tmp_path, bars, [*fills, gap])
    assert result.seen == 11
    assert len(result.compared) == 10
    assert result.unmatched == (gap,)
    lines = gate.report(result)
    assert lines[1:] == [
        "   no bar: 2026-10-12T13:45:10.500Z (+10.500s) MNQ 12-26 qty=1 price=30005.0",
        "   1 unmatched, 0 allowed: explain each in docs/data-gate.md Item 5",
        "FAIL",  # an unchecked fill is not a pass, though 10 others were within tolerance
    ]
    assert not gate.passed(result)


def test_most_fills_unmatched_fails_however_far_off_they_are():
    # review of #253: 10 fills inside their bars and 30 at 99999 with no bar printed PASS
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0) for m in range(10)])
    inside = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    wild = [fill(f"2026-10-12T14:{m:02d}:30.000Z", 99999.0) for m in range(30)]
    result = gate.compare_fills([*inside, *wild], bars, SPECS)
    assert (result.seen, len(result.compared), len(result.unmatched)) == (40, 10, 30)
    assert not gate.passed(result)
    assert not gate.passed(result, allow_unmatched=29)
    assert gate.report(result)[-1] == "FAIL"


def test_allow_unmatched_accepts_up_to_n_and_still_lists_each():
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0) for m in range(10)])
    inside = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    gaps = [fill(f"2026-10-12T14:0{m}:01.000Z", 30005.0) for m in range(2)]
    result = gate.compare_fills([*inside, *gaps], bars, SPECS)
    assert not gate.passed(result, allow_unmatched=1)
    assert gate.passed(result, allow_unmatched=2)
    lines = gate.report(result, allow_unmatched=2)
    assert lines[1:] == [
        "   no bar: 2026-10-12T14:00:01.000Z (+1.000s) MNQ 12-26 qty=1 price=30005.0",
        "   no bar: 2026-10-12T14:01:01.000Z (+1.000s) MNQ 12-26 qty=1 price=30005.0",
        "   2 unmatched, 2 allowed: explain each in docs/data-gate.md Item 5",
        "PASS",
    ]


def test_a_fill_matches_only_its_own_contract_month():
    # NQH27 bars never stand in for an MNQ 12-26 fill: a month mismatch is unmatched
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0, "NQH27") for m in range(10)])
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    result = gate.compare_fills(fills, bars, SPECS)
    assert (len(result.compared), len(result.unmatched)) == (0, 10)
    assert not gate.passed(result, allow_unmatched=10)  # and nothing compared


def test_contract_of_maps_ninjatraders_name_to_our_code():
    assert gate.contract_of("MNQ 12-26", SPECS) == ("NQZ26", SPECS["MNQ"])
    assert gate.contract_of("ES 03-27", SPECS) == ("ESH27", SPECS["ES"])
    assert gate.contract_of("MES 06-27", SPECS)[0] == "ESM27"
    assert gate.contract_of("NQ 09-26", SPECS)[0] == "NQU26"


@pytest.mark.parametrize("instrument", ["YM 12-26", "MNQ 11-26", "MNQ", "MNQ 12-2026"])
def test_an_unknown_instrument_is_a_clear_error(instrument):
    with pytest.raises(ValueError, match="not a quarterly contract of instruments.yaml"):
        gate.compare_fills([fill("2026-10-12T13:31:30.000Z", 1.0, instrument)], {}, SPECS)


def test_qty_0_fills_are_skipped_and_never_count_toward_ten():
    bars = gate.bars_by_minute([bar(m, 30000.0, 30010.0) for m in range(10)])
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(9)]
    fills += [fill("2026-10-12T13:09:30.000Z", 30005.0, qty=0)]
    result = gate.compare_fills(fills, bars, SPECS)
    assert (result.seen, len(result.compared), result.skipped) == (10, 9, 1)
    assert not gate.passed(result)


def test_a_fill_matches_only_its_own_series():
    bars = gate.bars_by_minute([bar(31, 30000.0, 30010.0, "NQZ26")])
    result = gate.compare_fills(
        [fill("2026-10-12T13:31:30.000Z", 7850.0, "MES 12-26")], bars, SPECS
    )
    assert len(result.compared) == 0
    assert len(result.unmatched) == 1


def test_load_reads_only_the_accounts_rows_of_the_day(tmp_path):
    db = journalled(tmp_path, [bar(31, 1.0, 2.0)], [fill("2026-10-12T13:31:30.000Z", 1.5)])
    conn = journal.open_db(db)
    journal.write(conn, utc(2026, 10, 12, 13, 32), "Other", "fill", fill("x", 1.0))
    journal.write(conn, utc(2026, 10, 13, 13, 32), "Sim101", "fill", fill("y", 1.0))  # next day
    conn.close()
    fills, bars = gate.load(db, "Sim101", [DAY])
    assert [f["ts"] for f in fills] == ["2026-10-12T13:31:30.000Z"]
    assert len(bars) == 1


def test_load_never_creates_a_missing_db(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        gate.load(tmp_path / "fenrir.db", "Sim101", [DAY])
    assert not (tmp_path / "fenrir.db").exists()


def test_main_prints_the_verdict(tmp_path, monkeypatch, capsys):
    bars = [bar(m, 30000.0, 30010.0) for m in range(10)]
    fills = [fill(f"2026-10-12T13:{m:02d}:30.000Z", 30005.0) for m in range(10)]
    db = journalled(tmp_path, bars, fills)
    argv = ["gate_item5.py", "Sim101", "--day", "2026-10-12", "--db", str(db)]
    monkeypatch.setattr(sys, "argv", [*argv, "--instruments", str(_root / "instruments.yaml")])
    gate.main()
    out = capsys.readouterr().out.splitlines()
    assert (
        out[1]
        == "fills seen=10 compared=10 unmatched=0 skipped=0 worst=0.00 ticks (tolerance 1 tick)"
    )
    assert out[-1] == "PASS"


def test_fills_and_bars_key_by_the_minute_in_utc_whatever_the_offset():
    bars = gate.bars_by_minute([bar(31, 30000.0, 30010.0)])
    result = gate.compare_fills([fill("2026-10-12T09:31:30.000-04:00", 30005.0)], bars, SPECS)
    assert len(result.compared) == 1


def test_main_fails_cleanly_on_an_unknown_instrument(tmp_path, monkeypatch):
    db = journalled(
        tmp_path, [bar(31, 1.0, 2.0)], [fill("2026-10-12T13:31:30.000Z", 1.5, "YM 12-26")]
    )
    argv = ["gate_item5.py", "Sim101", "--day", "2026-10-12", "--db", str(db)]
    monkeypatch.setattr(sys, "argv", [*argv, "--instruments", str(_root / "instruments.yaml")])
    with pytest.raises(SystemExit, match="FAIL: fill on instrument 'YM 12-26'"):
        gate.main()
