"""SQLite trade journal. Shell: every function does I/O on the connection."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fenrir.core.sessions import NY

KINDS = frozenset({"bar", "action", "rejection", "fill", "alert", "halt"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_account_ts ON journal (account, ts);
CREATE TABLE IF NOT EXISTS armed (
    account TEXT PRIMARY KEY,
    strategy_path TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    rules_account TEXT NOT NULL,
    contract TEXT NOT NULL,
    mode TEXT NOT NULL,
    phase TEXT NOT NULL,
    armed_at TEXT NOT NULL
);
"""


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
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(_SCHEMA)
    return conn


def write(
    conn: sqlite3.Connection, ts: datetime, account: str, kind: str, payload: Mapping[str, Any]
) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind {kind!r}")
    with conn:
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
) -> tuple[JournalRow, ...]:
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
    return tuple(
        JournalRow(i, datetime.fromisoformat(ts), acc, kind, json.loads(p))
        for i, ts, acc, kind, p in conn.execute(sql + " ORDER BY id", args)
    )
