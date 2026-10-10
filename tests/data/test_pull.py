from datetime import date

from fenrir.core.instruments import parse_instruments
from fenrir.data.pull import Window, pull_windows, todo

INST = parse_instruments(
    {
        "contracts": [{"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1}],
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
                "NQZ25": {"first_day": "2025-12-01", "last_day": "2025-12-10", "rows": 9, "complete": True},
                "NQH26": {"first_day": "2025-12-11", "last_day": "2026-02-01", "rows": 9, "complete": False},
            }
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2026, 3, 20))] == ["NQH26", "NQM26"]


def test_todo_repulls_when_since_moves_earlier():
    windows = pull_windows(INST, "NQ", date(2025, 11, 1), date(2025, 12, 5))
    manifest = {
        "series": {
            "NQ": {"NQZ25": {"first_day": "2025-12-01", "last_day": "2025-12-10", "rows": 9, "complete": True}}
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2025, 12, 5))] == ["NQZ25"]


def test_todo_empty_manifest_pulls_everything():
    windows = pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20))
    assert todo(windows, {}, date(2026, 3, 20)) == windows
