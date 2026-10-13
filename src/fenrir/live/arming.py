"""Armed-strategy storage and the pure arm decision. The only writer of the `armed` table."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Literal, get_args

from fenrir.backtest.report import Report
from fenrir.backtest.run import AccountChoice, net_per_attempt
from fenrir.live import journal
from fenrir.live.journal import iso
from fenrir.rules.account import phase_rules
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import Phase


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
    # pinned at arming (DEC-4d); None in a row armed before plan 23.3, which never starts
    dll_on: bool | None = None  # accounts.yaml's, which the matched backtest used
    rules_hash: str | None = None  # sha256 of the rules file
    instruments_hash: str | None = None  # sha256 of instruments.yaml
    # NinjaTrader balances at phase start and the peak so far (DEC-4a); None for dry runs
    start_balance: float | None = None
    peak_balance: float | None = None


_COLUMNS = tuple(f.name for f in fields(Armed))
DISARMED = {"reason": "disarmed", "kind": None, "until": None}


def list_armed(conn: sqlite3.Connection) -> tuple[Armed, ...]:
    """By column name: the migration appends columns, and a reader on a db no `open_db` has
    migrated yet (an `open_ro` right after an upgrade) reads the missing pins as None."""
    cur = conn.execute("SELECT * FROM armed ORDER BY account")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r, strict=True)) for r in cur]
    return tuple(
        Armed(
            **{c: r.get(c) for c in _COLUMNS}
            | {
                "armed_at": datetime.fromisoformat(r["armed_at"]),
                "dll_on": None if r.get("dll_on") is None else bool(r["dll_on"]),
            }
        )
        for r in rows
    )


def insert_armed(conn: sqlite3.Connection, a: Armed) -> None:
    """Insert or replace: one armed strategy per account."""
    row = asdict(a) | {"armed_at": iso(a.armed_at)}
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO armed ({', '.join(_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(_COLUMNS))})",
            [row[c] for c in _COLUMNS],
        )


def delete_armed(conn: sqlite3.Connection, account: str) -> None:
    with conn:
        conn.execute("DELETE FROM armed WHERE account = ?", (account,))


def disarm(conn: sqlite3.Connection, account: str, now: datetime) -> bool:
    """Journal the `"halt"` row and delete the `armed` row in one transaction, so a failure
    leaves both or neither; False (nothing written) when the account is not armed."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM armed WHERE account = ?", (account,)).fetchone() is None:
            return False
        journal.insert(conn, now, account, "halt", DISARMED)
        conn.execute("DELETE FROM armed WHERE account = ?", (account,))
    return True


def liquidation_level(
    rules: AccountRules, phase: Phase, start_balance: float, peak_balance: float
) -> float:
    """The drawdown floor in NinjaTrader's dollars that the runner starts from with these
    balances (`core.start_account`), to check against the firm's dashboard (DEC-4a)."""
    dd = phase_rules(rules, phase).max_drawdown
    peak = peak_balance - start_balance
    level = peak if dd.locks_at is None else min(peak, dd.locks_at)
    return start_balance + max(-dd.amount, level - dd.amount)


Mode = Literal["dry", "sim", "live"]


@dataclass(frozen=True, slots=True)
class ArmRequest:
    account: str
    strategy_path: str
    rules_account: str
    contract: str
    mode: Mode
    phase: Phase
    confirm: bool
    start_balance: float | None = None  # required for sim and live (DEC-4a)
    peak_balance: float | None = None
    # a live confirm echoes what the summary showed; any other value is no confirmation
    strategy_hash: str | None = None
    rules_hash: str | None = None
    dll_on: bool | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    error: str  # a 409 reason from the arming contract
    summary: dict[str, Any] | None = None


def decide(
    req: ArmRequest,
    armed: Sequence[Armed],
    strategy_name: str,
    strategy_hash: str,
    rules_hash: str,
    instruments_hash: str,
    reports: Sequence[Report],
    is_sim: bool | None,
    rules: AccountRules,
    choices: Mapping[str, AccountChoice],
    now: datetime,
) -> Armed | Refusal:
    """May this strategy file be armed on this account?

    `armed` from `list_armed`; the hashes are of the strategy file, the rules file `rules` was
    read from, and instruments.yaml, as they are now; `reports` newest first; `is_sim` None =
    NinjaTrader does not report the account; `choices` from `load_account_choices`.
    """
    # fail closed: an unchecked "Live" would skip both the mode check and the confirmation
    if req.mode not in get_args(Mode) or req.phase not in get_args(Phase):
        raise ValueError(f"bad mode {req.mode!r} or phase {req.phase!r}")
    if req.mode != "dry" and (req.start_balance is None or req.peak_balance is None):
        raise ValueError("sim and live need start_balance and peak_balance")
    if any(a.account == req.account for a in armed):
        return Refusal("already_armed")
    if req.mode != "dry":
        if is_sim is None:
            return Refusal("unknown_account")
        if is_sim != (req.mode == "sim"):
            return Refusal("mode_mismatch")
    if req.phase == "evaluation" and rules.evaluation is None:
        return Refusal("no_evaluation_phase")
    same = [
        r
        for r in reports
        if r["strategy"] == strategy_name
        and r["account"] == req.rules_account
        and r["contract"] == req.contract
    ]
    current = [r for r in same if r["strategy_hash"] == strategy_hash]
    if not current:
        return Refusal("hash_changed" if same else "no_backtest")
    current = [r for r in current if r["rules_hash"] == rules_hash]
    if not current:
        return Refusal("rules_changed")
    dll_on = choices.get(req.rules_account, AccountChoice()).dll_on
    match = next((r for r in current if r["dll_on"] == dll_on), None)
    if match is None:
        return Refusal("dll_mismatch")
    shown = (strategy_hash, rules_hash, dll_on)
    if req.mode == "live" and (
        not req.confirm or (req.strategy_hash, req.rules_hash, req.dll_on) != shown
    ):
        tuning = match["periods"]["tuning"]
        return Refusal(
            "needs_confirmation",
            {
                **tuning,
                "expected_net_per_attempt": net_per_attempt(tuning, req.rules_account, choices),
                "holdout_gap": match["holdout_gap"],
                "strategy_hash": strategy_hash,
                "rules_hash": rules_hash,
                "dll_on": dll_on,
                "liquidation_level": liquidation_level(
                    rules, req.phase, req.start_balance, req.peak_balance
                ),
            },
        )
    dry = req.mode == "dry"  # dry runs start fresh: no balances (DEC-4a)
    return Armed(
        req.account,
        req.strategy_path,
        strategy_hash,
        req.rules_account,
        req.contract,
        req.mode,
        req.phase,
        now,
        dll_on=dll_on,
        rules_hash=rules_hash,
        instruments_hash=instruments_hash,
        start_balance=None if dry else req.start_balance,
        peak_balance=None if dry else req.peak_balance,
    )
