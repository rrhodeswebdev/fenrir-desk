import os
from datetime import date
from pathlib import Path

import pytest

from fenrir.core import instruments
from fenrir.core.instruments import (
    ContractSpec,
    Roll,
    front_month,
    in_roll_window,
    load_instruments,
    parse_instruments,
    quarterly_rolls,
    spec,
)

DOC = {
    "contracts": [
        {"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1},
        {"symbol": "MNQ", "series": "NQ", "point_value": 2, "tick": 0.25, "minis": 0.1},
    ],
    "rollover": {"series": ["NQ"], "first_year": 2025, "last_year": 2026},
}


def test_quarterly_rolls_dates_and_codes():
    rolls = quarterly_rolls("NQ", 2025, 2025)
    assert rolls[0] == Roll("NQ", date(2025, 1, 1), "NQH25")
    # third Fridays 2025: Mar 21, Jun 20, Sep 19, Dec 19 -> minus 8 days
    assert rolls[1:] == (
        Roll("NQ", date(2025, 3, 13), "NQM25"),
        Roll("NQ", date(2025, 6, 12), "NQU25"),
        Roll("NQ", date(2025, 9, 11), "NQZ25"),
        Roll("NQ", date(2025, 12, 11), "NQH26"),
    )
    assert all(r.start.weekday() == 3 for r in rolls[1:])  # Thursdays


def test_front_month_switches_on_roll_day():
    inst = parse_instruments(DOC)
    assert front_month(inst, "NQ", date(2025, 12, 10)) == "NQZ25"
    assert front_month(inst, "NQ", date(2025, 12, 11)) == "NQH26"
    assert front_month(inst, "NQ", date(2026, 3, 12)) == "NQM26"


def test_front_month_before_calendar_raises():
    with pytest.raises(ValueError, match="NQ"):
        front_month(parse_instruments(DOC), "NQ", date(2024, 12, 31))


def test_front_month_past_calendar_raises():
    inst = parse_instruments(DOC)  # last roll 2026-12-10 -> NQH27, which rolls 2027-03-11
    assert front_month(inst, "NQ", date(2027, 3, 10)) == "NQH27"
    with pytest.raises(ValueError, match="calendar ends 2027-03-11"):
        front_month(inst, "NQ", date(2027, 3, 11))


def test_front_month_unknown_series_raises():
    with pytest.raises(ValueError, match="ES"):
        front_month(parse_instruments(DOC), "ES", date(2025, 6, 1))


def test_spec_lookup():
    inst = parse_instruments(DOC)
    assert spec(inst, "MNQ") == ContractSpec("MNQ", "NQ", 2.0, 0.25, 0.1)
    with pytest.raises(ValueError, match="XYZ"):
        spec(inst, "XYZ")


def test_repo_instruments_file_matches_spec():
    inst = load_instruments(Path(__file__).parents[2] / "instruments.yaml")
    assert {s.symbol: (s.series, s.point_value, s.tick, s.minis) for s in inst.specs} == {
        "ES": ("ES", 50.0, 0.25, 1.0),
        "MES": ("ES", 5.0, 0.25, 0.1),
        "NQ": ("NQ", 20.0, 0.25, 1.0),
        "MNQ": ("NQ", 2.0, 0.25, 0.1),
    }
    assert front_month(inst, "ES", date(2026, 10, 9)) == "ESZ26"


def test_in_roll_window_is_roll_thursday_through_expiry_tuesday():
    # Dec 2025: expiry Fri 12-19, roll Thu 12-11; window ends Tue 12-16
    assert [d for d in range(8, 20) if in_roll_window(date(2025, 12, d))] == [
        11,
        12,
        13,
        14,
        15,
        16,
    ]
    assert not in_roll_window(date(2025, 11, 13))  # not a quarterly month


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversized"])
def test_load_instruments_refuses_a_planted_file(tmp_path, monkeypatch, kind):
    # the terminal can write the repo; the root server and pull read this file (DEC-1)
    real = Path(__file__).parents[2] / "instruments.yaml"
    path = tmp_path / "instruments.yaml"
    if kind == "symlink":
        path.symlink_to(real)
    elif kind == "fifo":
        os.mkfifo(path)  # a blocking open would hang
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(real.read_bytes())
        monkeypatch.setattr(instruments, "INSTRUMENTS_MAX", path.stat().st_size - 1)
    message = {"symlink": "is a symlink", "oversized": "is over"}.get(kind, "is not a regular file")
    with pytest.raises(ValueError, match=f"{path} {message}"):
        load_instruments(path)
