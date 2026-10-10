import json
from datetime import UTC, date, datetime, time

import polars as pl
import pytest

from fenrir.core.instruments import parse_instruments
from fenrir.data.pull import Window, pull, pull_windows, todo
from fenrir.data.store import load_bars

INST = parse_instruments(
    {
        "contracts": [
            {"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1}
        ],
        "rollover": {"series": ["NQ"], "first_year": 2025, "last_year": 2026},
    }
)


def test_pull_windows_cover_since_to_today_by_contract():
    assert pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20)) == (
        Window("NQ", "NQZ25", date(2025, 12, 1), date(2025, 12, 10)),
        Window("NQ", "NQH26", date(2025, 12, 11), date(2026, 3, 11)),
        Window("NQ", "NQM26", date(2026, 3, 12), date(2026, 3, 20)),
    )


def test_todo_skips_complete_contracts_only():
    windows = pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20))
    manifest = {
        "series": {
            "NQ": {
                "NQZ25": {
                    "first_day": "2025-12-01",
                    "last_day": "2025-12-10",
                    "rows": 9,
                    "complete": True,
                },
                "NQH26": {
                    "first_day": "2025-12-11",
                    "last_day": "2026-02-01",
                    "rows": 9,
                    "complete": False,
                },
            }
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2026, 3, 20))] == ["NQH26", "NQM26"]


def test_todo_repulls_when_since_moves_earlier():
    windows = pull_windows(INST, "NQ", date(2025, 11, 1), date(2025, 12, 5))
    manifest = {
        "series": {
            "NQ": {
                "NQZ25": {
                    "first_day": "2025-12-01",
                    "last_day": "2025-12-10",
                    "rows": 9,
                    "complete": True,
                }
            }
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2025, 12, 5))] == ["NQZ25"]


def test_todo_empty_manifest_pulls_everything():
    windows = pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20))
    assert todo(windows, {}, date(2026, 3, 20)) == windows


def fake_fetch(w: Window) -> pl.DataFrame:
    ts = datetime.combine(w.last, time(15, 0), tzinfo=UTC)
    return pl.DataFrame(
        {"ts": [ts], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]}
    )


def test_pull_writes_contracts_and_manifest(tmp_path):
    done = pull(
        tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), fake_fetch, lambda: "t"
    )
    assert [w.contract for w in done] == ["NQZ25", "NQH26", "NQM26"]
    manifest = json.loads((tmp_path / "bars" / "manifest.json").read_text())
    assert sorted(manifest["series"]["NQ"]) == ["NQH26", "NQM26", "NQZ25"]
    assert len(load_bars(tmp_path, INST, "NQ")) == 3
    # second run only re-pulls the incomplete current contract
    again = pull(
        tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), fake_fetch, lambda: "t"
    )
    assert [w.contract for w in again] == ["NQM26"]


def test_pull_failure_keeps_finished_contracts(tmp_path):
    def flaky(w: Window) -> pl.DataFrame:
        if w.contract == "NQH26":
            raise ConnectionError("dropped")
        return fake_fetch(w)

    with pytest.raises(ConnectionError):
        pull(tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), flaky, lambda: "t")
    manifest = json.loads((tmp_path / "bars" / "manifest.json").read_text())
    assert list(manifest["series"]["NQ"]) == ["NQZ25"]
