import sqlite3
from contextlib import closing
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


def test_a_new_db_appears_with_its_tables(tmp_path, monkeypatch):
    """#179: a reader that sees the file never sees it without tables (first arm vs journal)."""
    path = tmp_path / "private" / "fenrir.db"
    seen = []
    link = journal.os.link

    def check(src, dst):
        with sqlite3.connect(src) as c:
            seen.append({r[0] for r in c.execute("SELECT name FROM sqlite_master")})
        assert not path.exists()
        link(src, dst)

    monkeypatch.setattr(journal.os, "link", check)
    journal.open_db(path).close()
    assert {"journal", "armed"} <= seen[0]
    assert [p.name for p in path.parent.iterdir()] == ["fenrir.db"]  # no temp file left
    journal.open_db(path).close()  # an existing db is opened in place
    assert len(seen) == 1


def test_unknown_kind_is_rejected(conn):
    with pytest.raises(ValueError, match="kind"):
        journal.write(conn, datetime.now(UTC), "Sim101", "chatter", {})


def test_rows_filter_by_account_and_trading_day(conn):
    # trading day 2026-10-12 runs 2026-10-11 18:00 ET (22:00 UTC) to 2026-10-12 18:00 ET,
    # its 17:00-18:00 break included (as core.sessions.trading_day counts it)
    journal.write(conn, datetime(2026, 10, 11, 21, 59, tzinfo=UTC), "Sim101", "bar", {"n": 0})
    journal.write(conn, datetime(2026, 10, 11, 22, 0, tzinfo=UTC), "Sim101", "bar", {"n": 1})
    journal.write(conn, datetime(2026, 10, 12, 20, 59, tzinfo=UTC), "Sim101", "bar", {"n": 2})
    journal.write(conn, datetime(2026, 10, 12, 21, 30, tzinfo=UTC), "Sim101", "halt", {"n": 3})
    journal.write(conn, datetime(2026, 10, 12, 22, 0, tzinfo=UTC), "Sim101", "bar", {"n": 4})
    journal.write(conn, datetime(2026, 10, 12, 14, 0, tzinfo=UTC), "Other", "bar", {"n": 5})
    got = journal.rows(conn, account="Sim101", day=date(2026, 10, 12))
    assert [r.payload["n"] for r in got] == [1, 2, 3]
    assert [r.payload["n"] for r in journal.rows(conn, day=date(2026, 10, 12))] == [1, 2, 3, 5]


def test_rows_filter_by_kind_and_since(conn):
    journal.write(conn, ARMED_AT, "Sim101", "halt", {"n": 0})
    journal.write(conn, ARMED_AT, "Sim101", "bar", {"n": 1})
    journal.write(conn, datetime(2026, 10, 12, 12, 59, tzinfo=UTC), "Sim101", "halt", {"n": 2})
    got = journal.rows(conn, account="Sim101", kind="halt", since=ARMED_AT)
    assert [r.payload["n"] for r in got] == [0]


def test_day_bounds_follow_winter_time():
    assert journal.day_bounds(date(2026, 12, 15)) == (
        datetime(2026, 12, 14, 23, 0, tzinfo=UTC),
        datetime(2026, 12, 15, 23, 0, tzinfo=UTC),
    )


def test_naive_timestamp_is_rejected(conn):
    with pytest.raises(ValueError, match="naive"):
        journal.write(conn, datetime(2026, 10, 12, 12, 0), "Sim101", "bar", {})


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


def test_a_reader_never_blocks_a_write(tmp_path):
    """WAL: GET /api/live/journal reading must never stall the hub's journal writes."""
    path = tmp_path / "fenrir.db"
    conn = journal.open_db(path)
    conn.execute("PRAGMA busy_timeout = 0")  # a lock would raise at once
    reader = journal.open_ro(path)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM journal").fetchone() == (0,)  # holds its snapshot
    journal.write(conn, ARMED_AT, "Sim101", "bar", {})
    assert reader.execute("SELECT count(*) FROM journal").fetchone() == (0,)
    reader.close()
    assert len(journal.rows(journal.open_ro(path))) == 1


def test_read_only_never_creates_the_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        journal.open_ro(tmp_path / "fenrir.db")
    assert not (tmp_path / "fenrir.db").exists()


OLD_ARMED = """CREATE TABLE armed (
    account TEXT PRIMARY KEY, strategy_path TEXT NOT NULL, strategy_hash TEXT NOT NULL,
    rules_account TEXT NOT NULL, contract TEXT NOT NULL, mode TEXT NOT NULL, phase TEXT NOT NULL,
    armed_at TEXT NOT NULL)"""
PINNED = Armed(
    "Sim101",
    "strategies/orb.py",
    "h1",
    "lucid-flex-50k",
    "MNQ",
    "sim",
    "evaluation",
    ARMED_AT,
    dll_on=True,
    rules_hash="r1",
    instruments_hash="i1",
    start_balance=50_000.0,
    peak_balance=50_500.0,
)


def test_an_armed_table_from_before_23_3_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS never adds a column: open_db adds the pinned ones (DEC-4d)."""
    path = tmp_path / "fenrir.db"
    with closing(sqlite3.connect(path)) as old, old:
        old.execute(OLD_ARMED)
        old.execute(
            "INSERT INTO armed VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Sim101", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "dry", "funded",
             journal.iso(ARMED_AT)),
        )  # fmt: skip
    # a reader before any open_db (GET /api/arming right after an upgrade) still lists it
    with closing(journal.open_ro(path)) as ro:
        assert arming.list_armed(ro)[0].rules_hash is None
    conn = journal.open_db(path)
    # the row armed before the pins came reads as unpinned, which the runner refuses to start
    [old_row] = arming.list_armed(conn)
    assert old_row == Armed(
        "Sim101", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "dry", "funded", ARMED_AT
    )
    assert (old_row.dll_on, old_row.rules_hash, old_row.start_balance) == (None, None, None)
    journal.open_db(path).close()  # migrating twice is a no-op
    arming.insert_armed(conn, replace(PINNED, account="Sim102"))
    assert arming.list_armed(conn)[1] == replace(PINNED, account="Sim102")


def test_armed_rows_are_read_and_written_by_column_name(tmp_path):
    """The migration appends columns: insert and list must not depend on their order."""
    path = tmp_path / "fenrir.db"
    with closing(sqlite3.connect(path)) as c, c:
        c.execute(
            "CREATE TABLE armed (peak_balance REAL, dll_on INTEGER, armed_at TEXT NOT NULL,"
            " instruments_hash TEXT, phase TEXT NOT NULL, mode TEXT NOT NULL, contract TEXT NOT"
            " NULL, start_balance REAL, rules_account TEXT NOT NULL, strategy_hash TEXT NOT NULL,"
            " rules_hash TEXT, strategy_path TEXT NOT NULL, account TEXT PRIMARY KEY)"
        )
    conn = journal.open_db(path)
    arming.insert_armed(conn, PINNED)
    [got] = arming.list_armed(conn)
    assert got == PINNED and got.dll_on is True  # a bool, not sqlite's 1


def test_two_writers_migrating_at_once_both_open(tmp_path):
    """The hub and the arm route can open an old db together: the second sees the columns."""
    path = tmp_path / "fenrir.db"
    with closing(sqlite3.connect(path)) as old, old:
        old.execute(OLD_ARMED)
    first = journal.open_db(path)
    second = journal.open_db(path)
    arming.insert_armed(first, PINNED)
    assert arming.list_armed(second) == (PINNED,)


def test_disarm_journals_the_halt_and_deletes_the_row_in_one_transaction(conn):
    arming.insert_armed(conn, PINNED)
    # a delete that fails after the halt row went in must take the halt row with it
    conn.execute(
        "CREATE TRIGGER no_delete BEFORE DELETE ON armed BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        arming.disarm(conn, "Sim101", ARMED_AT)
    assert journal.rows(conn) == () and arming.list_armed(conn) == (PINNED,)
    conn.execute("DROP TRIGGER no_delete")
    assert arming.disarm(conn, "Sim101", ARMED_AT) is True
    assert arming.list_armed(conn) == ()
    [row] = journal.rows(conn)
    assert (row.kind, row.payload) == ("halt", {"reason": "disarmed", "kind": None, "until": None})
    assert arming.disarm(conn, "Sim101", ARMED_AT) is False  # not armed: nothing journalled
    assert len(journal.rows(conn)) == 1
