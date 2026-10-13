"""SQLite trade journal. Shell: every function does I/O on the connection."""

import contextlib
import json
import os
import sqlite3
import uuid
from collections.abc import Collection, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fenrir.core.sessions import NY

KINDS = frozenset({"bar", "action", "rejection", "fill", "alert", "halt", "peak", "day_start"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_account_ts ON journal (account, ts);
-- the all-accounts day read (the Live tape, every 5 s): without it a day scans the whole history
CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts);
CREATE TABLE IF NOT EXISTS armed (
    account TEXT PRIMARY KEY,
    strategy_path TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    rules_account TEXT NOT NULL,
    contract TEXT NOT NULL,
    mode TEXT NOT NULL,
    phase TEXT NOT NULL,
    armed_at TEXT NOT NULL,
    dll_on INTEGER,
    rules_hash TEXT,
    instruments_hash TEXT,
    start_balance REAL,
    peak_balance REAL
);
"""
# plan 23.3 (DEC-4d): columns CREATE TABLE IF NOT EXISTS never adds to an older `armed`; NULL in
# a row armed before them, which the runner refuses to start
PINNED = (
    ("dll_on", "INTEGER"),
    ("rules_hash", "TEXT"),
    ("instruments_hash", "TEXT"),
    ("start_balance", "REAL"),
    ("peak_balance", "REAL"),
)


@dataclass(frozen=True, slots=True)
class JournalRow:
    id: int
    ts: datetime
    account: str
    kind: str
    payload: dict


def iso(ts: datetime) -> str:
    # one format everywhere, so string comparison orders by time
    if ts.tzinfo is None:
        raise ValueError(f"naive datetime: {ts!r}")
    return ts.astimezone(UTC).isoformat()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # built aside and linked in, so a reader never sees the file without its tables (#179);
        # link, not replace: never clobbers a db another writer created meanwhile
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with closing(sqlite3.connect(tmp)) as c:
                c.executescript(_SCHEMA)
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")  # readers (`open_ro`) never block the hub's writes
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _missing(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    have = {row[1] for row in conn.execute("PRAGMA table_info(armed)")}
    return [(name, kind) for name, kind in PINNED if name not in have]


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the pinned columns to an `armed` table from before plan 23.3."""
    if not _missing(conn):
        return
    with conn:
        # the write lock, then look again: the hub and the arm route may both be migrating
        conn.execute("BEGIN IMMEDIATE")
        for name, kind in _missing(conn):
            conn.execute(f"ALTER TABLE armed ADD COLUMN {name} {kind}")


def open_ro(path: Path) -> sqlite3.Connection:
    """A read-only connection to an existing db (sqlite3.OperationalError if missing): the
    journal API reads on its own, never on the hub's connection, and never creates the file.
    The db is in WAL mode, so even a read-only reader may create `-wal`/`-shm` beside it: the
    private dir must be writable by the server."""
    return sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)


def write(
    conn: sqlite3.Connection, ts: datetime, account: str, kind: str, payload: Mapping[str, Any]
) -> None:
    with conn:
        insert(conn, ts, account, kind, payload)


def insert(
    conn: sqlite3.Connection, ts: datetime, account: str, kind: str, payload: Mapping[str, Any]
) -> None:
    """`write` inside the caller's transaction: commits nothing."""
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind {kind!r}")
    conn.execute(
        "INSERT INTO journal (ts, account, kind, payload_json) VALUES (?, ?, ?, ?)",
        (iso(ts), account, kind, json.dumps(payload, default=str)),
    )


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC [18:00 ET on day-1, 18:00 ET on day): the trading day plus its 17:00 break, as
    `core.sessions.trading_day` counts it, so a halt journalled in the break is not lost."""
    start = datetime.combine(day - timedelta(days=1), time(18), NY)
    end = datetime.combine(day, time(18), NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def rows(
    conn: sqlite3.Connection,
    account: str | None = None,
    day: date | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    skip: Collection[str] = (),
) -> tuple[JournalRow, ...]:
    """Rows by id; `skip` leaves those kinds out in SQL (never read nor decoded)."""
    sql, args = select(account, day, kind, since, skip)
    return tuple(
        JournalRow(i, datetime.fromisoformat(ts), acc, kind, json.loads(p))
        for i, ts, acc, kind, p in conn.execute(sql, args)
    )


def select(
    account: str | None = None,
    day: date | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    skip: Collection[str] = (),
) -> tuple[str, list[str]]:
    """The parameterised query `rows` runs (apart so a test can read its query plan)."""
    sql, args = "SELECT id, ts, account, kind, payload_json FROM journal WHERE 1=1", []
    if account is not None:
        sql += " AND account = ?"
        args.append(account)
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if since is not None:
        sql += " AND ts >= ?"
        args.append(iso(since))
    if day is not None:
        start, end = day_bounds(day)
        sql += " AND ts >= ? AND ts < ?"
        args += [iso(start), iso(end)]
    if skip:
        sql += f" AND kind NOT IN ({', '.join('?' * len(skip))})"
        args += list(skip)
    return sql + " ORDER BY id", args
