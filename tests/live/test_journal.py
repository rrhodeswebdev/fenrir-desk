from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from live_helpers import ARMED_AT

from fenrir.live import arming, journal
from fenrir.live.arming import Armed


@pytest.fixture
def conn(tmp_path):
    return journal.open_db(tmp_path / "data" / "fenrir.db")


def test_write_and_read_back(conn):
    ts = datetime(2026, 10, 12, 13, 31, tzinfo=UTC)
    journal.write(conn, ts, "Sim101", "action", {"type": "Bracket", "qty": 1})
    [row] = journal.rows(conn)
    assert (row.ts, row.account, row.kind, row.payload) == (
        ts,
        "Sim101",
        "action",
        {"type": "Bracket", "qty": 1},
    )


def test_unknown_kind_is_rejected(conn):
    with pytest.raises(ValueError, match="kind"):
        journal.write(conn, datetime.now(UTC), "Sim101", "chatter", {})


def test_rows_filter_by_account_and_trading_day(conn):
    # trading day 2026-10-12 runs 2026-10-11 18:00 ET (22:00 UTC) to 2026-10-12 17:00 ET (21:00 UTC)
    journal.write(conn, datetime(2026, 10, 11, 21, 59, tzinfo=UTC), "Sim101", "bar", {"n": 0})
    journal.write(conn, datetime(2026, 10, 11, 22, 0, tzinfo=UTC), "Sim101", "bar", {"n": 1})
    journal.write(conn, datetime(2026, 10, 12, 20, 59, tzinfo=UTC), "Sim101", "bar", {"n": 2})
    journal.write(conn, datetime(2026, 10, 12, 21, 0, tzinfo=UTC), "Sim101", "bar", {"n": 3})
    journal.write(conn, datetime(2026, 10, 12, 14, 0, tzinfo=UTC), "Other", "bar", {"n": 4})
    got = journal.rows(conn, account="Sim101", day=date(2026, 10, 12))
    assert [r.payload["n"] for r in got] == [1, 2]


def test_rearming_replaces_and_delete_removes(conn):
    a = Armed(
        "Sim101", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "dry", "evaluation", ARMED_AT
    )
    arming.insert_armed(conn, a)
    arming.insert_armed(conn, replace(a, strategy_hash="h2", mode="sim"))
    assert arming.list_armed(conn) == (replace(a, strategy_hash="h2", mode="sim"),)
    arming.delete_armed(conn, "Sim101")
    assert arming.list_armed(conn) == ()


def test_reopening_keeps_existing_rows(tmp_path):
    path = tmp_path / "fenrir.db"
    a = Armed(
        "Sim101", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "dry", "funded", ARMED_AT
    )
    arming.insert_armed(journal.open_db(path), a)
    journal.write(journal.open_db(path), ARMED_AT, "Sim101", "halt", {"reason": "x"})
    conn = journal.open_db(path)
    assert arming.list_armed(conn) == (a,)
    assert [r.kind for r in journal.rows(conn)] == ["halt"]
