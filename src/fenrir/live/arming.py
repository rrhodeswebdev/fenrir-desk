"""Armed-strategy storage. The only writer of the `armed` table; plan 23 adds the arm checks."""

import sqlite3
from dataclasses import astuple, dataclass
from datetime import datetime

from fenrir.live.journal import iso


@dataclass(frozen=True, slots=True)
class Armed:
    account: str
    strategy_path: str
    strategy_hash: str
    rules_account: str
    contract: str
    mode: str  # "dry" | "sim" | "live"
    phase: str  # "evaluation" | "funded"
    armed_at: datetime


def list_armed(conn: sqlite3.Connection) -> tuple[Armed, ...]:
    return tuple(
        Armed(*r[:7], datetime.fromisoformat(r[7]))
        for r in conn.execute(
            "SELECT account, strategy_path, strategy_hash, rules_account, contract, mode, phase,"
            " armed_at FROM armed ORDER BY account"
        )
    )


def insert_armed(conn: sqlite3.Connection, a: Armed) -> None:
    """Insert or replace: one armed strategy per account."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO armed VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*astuple(a)[:7], iso(a.armed_at)),
        )


def delete_armed(conn: sqlite3.Connection, account: str) -> None:
    with conn:
        conn.execute("DELETE FROM armed WHERE account = ?", (account,))
