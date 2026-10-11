"""Armed-strategy storage and the pure arm decision. The only writer of the `armed` table."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass
from datetime import datetime
from typing import Any, Literal

from fenrir.backtest.report import Report
from fenrir.backtest.run import AccountChoice, net_per_attempt
from fenrir.live.journal import iso
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


@dataclass(frozen=True, slots=True)
class Refusal:
    error: str  # a 409 reason from the arming contract
    summary: dict[str, Any] | None = None


def decide(
    req: ArmRequest,
    armed: Sequence[Armed],
    strategy_name: str,
    strategy_hash: str,
    reports: Sequence[Report],
    is_sim: bool | None,
    has_evaluation: bool,
    choices: Mapping[str, AccountChoice],
    now: datetime,
) -> Armed | Refusal:
    """May this strategy file be armed on this account?

    `armed` from `list_armed`; `reports` newest first; `is_sim` None = NinjaTrader does not
    report the account; `has_evaluation` False for account types without one (Lucid Direct);
    `choices` from `load_account_choices`.
    """
    if any(a.account == req.account for a in armed):
        return Refusal("already_armed")
    if req.mode != "dry":
        if is_sim is None:
            return Refusal("unknown_account")
        if is_sim != (req.mode == "sim"):
            return Refusal("mode_mismatch")
    if req.phase == "evaluation" and not has_evaluation:
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
    dll_on = choices.get(req.rules_account, AccountChoice()).dll_on
    match = next((r for r in current if r["dll_on"] == dll_on), None)
    if match is None:
        return Refusal("dll_mismatch")
    if req.mode == "live" and not req.confirm:
        tuning = match["periods"]["tuning"]
        net = net_per_attempt(tuning, req.rules_account, choices)
        return Refusal("needs_confirmation", {**tuning, "expected_net_per_attempt": net})
    return Armed(
        req.account,
        req.strategy_path,
        strategy_hash,
        req.rules_account,
        req.contract,
        req.mode,
        req.phase,
        now,
    )
