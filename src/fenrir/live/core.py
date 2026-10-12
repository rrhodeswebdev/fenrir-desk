"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O or clock.

Per closed bar the shell calls `on_bar`, runs the strategy's `step` on `ctx(state)` in its own child
process (DEC-1), then passes the returned actions to `decide` (or the error to `step_failed`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fenrir.backtest.attempt import ADDON_FLATTEN, MINUTE, WINDOW
from fenrir.core.actions import Action, Bracket
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import at_or_after, trading_day
from fenrir.live.arming import Armed
from fenrir.rules.account import initial_state, phase_rules, view
from fenrir.rules.contracts import max_minis
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, Phase, at_or_below
from fenrir.strategy import FLAT, Ctx, Position


@dataclass(frozen=True, slots=True)
class Start:
    """NinjaTrader balances in absolute dollars for a sim or live account (DEC-4a)."""

    phase_start: float  # balance when the current phase began (armed.start_balance)
    peak: float  # highest end-of-day balance (end_of_day) or intraday equity (intraday) so far
    balance: float  # balance now


@dataclass(frozen=True, slots=True)
class Setup:
    armed: Armed
    strategy_hash: str  # file_hash of the strategy file on disk now
    rules: AccountRules  # with_dll already applied
    spec: ContractSpec
    safety_margin_pct: float
    start: Start | None = None  # ignored for dry runs, which start fresh (DEC-4a)


@dataclass(frozen=True, slots=True)
class RunnerState:
    setup: Setup
    acct: AccountState
    position: Position
    bars: tuple[Bar, ...]  # last <= WINDOW bars, ending with the current bar
    equity: tuple[tuple[datetime, float], ...]  # close-mark equity per bar this trading day
    session_start: float  # equity at session start, relative to phase start
    session_floor: float  # drawdown floor at session start
    halted: str | None = None  # no new entries while set
    halt_kind: str | None = None  # "safety" | "breach" | ... (Plans 21–24 AccountStatus)
    halt_day: date | None = None  # halt lifts after this trading day; None = until disarmed


@dataclass(frozen=True, slots=True)
class Log:
    kind: str  # a fenrir.live.journal KINDS value
    payload: Mapping[str, Any]


Effect = Log
Step = tuple[RunnerState, tuple[Effect, ...]]


def start_account(
    rules: AccountRules, phase: Phase, day: date, start: Start | None
) -> AccountState:
    """State relative to phase start; with NinjaTrader balances the floor trails their peak."""
    fresh = initial_state(rules, phase, day)
    if start is None:
        return fresh
    dd = phase_rules(rules, phase).max_drawdown
    peak = start.peak - start.phase_start
    level = peak if dd.locks_at is None else min(peak, dd.locks_at)  # as drawdown.trail does
    balance = start.balance - start.phase_start
    return replace(
        fresh,
        balance=balance,
        day_start_balance=balance,
        peak=peak,
        floor=max(fresh.floor, level - dd.amount),
    )


def start(setup: Setup, now: datetime) -> Step:
    a = setup.armed
    begin = None if a.mode == "dry" else setup.start
    acct = start_account(setup.rules, a.phase, trading_day(now), begin)
    # shortcut: session start = balance at runner start; 21.5's warm-up restores today's fills
    s = RunnerState(setup, acct, FLAT, (), (), acct.balance, acct.floor)
    if setup.strategy_hash != a.strategy_hash:
        return halt(s, "strategy file changed since arming; re-run the backtest and re-arm")
    if a.mode != "dry" and setup.start is None:
        return halt(s, "sim and live need the NinjaTrader start and peak balances (DEC-4a)")
    return s, ()


def halt(s: RunnerState, reason: str) -> Step:
    """Halt until disarmed."""
    return _halt(s, reason, None, None)


def on_bar(s: RunnerState, bar: Bar) -> Step:
    # 21.4 (#78) rolls the trading day here, before anything else sees the bar.
    # 22.1 applies this bar's fills, then its Mark, here (backtest calling order).
    a = s.acct
    s = replace(
        s,
        bars=(*s.bars, bar)[-WINDOW:],
        equity=(*s.equity, (bar.ts, a.balance + a.open_pnl)),
    )
    return _safety(s)


def ctx(s: RunnerState) -> Ctx:
    """What `step` sees for the current bar."""
    return Ctx(
        ts=s.bars[-1].ts,
        bars=s.bars,
        position=s.position,
        account=view(s.acct, s.setup.rules),
        spec=s.setup.spec,
    )


def decide(s: RunnerState, actions: Sequence[Action]) -> Step:
    """Pre-trade check every action `step` returned for the current bar; nothing is sent yet.

    `actions` are Action values: the shell parses the child's JSON into them (DEC-1).
    """
    out = [Log("bar", {**asdict(s.bars[-1]), "actions": len(actions)})]
    pending = False  # a bracket accepted earlier this bar, as the backtest's `pending`
    for a in actions:
        reason = _block(s, a, pending)
        if reason:
            out.append(Log("rejection", {"action": _payload(a), "reason": reason}))
        else:
            out.append(Log("action", {**_payload(a), "mode": s.setup.armed.mode, "sent": False}))
        if not reason:
            pending = isinstance(a, Bracket)  # Flatten and Cancel clear it, as in the backtest
    return s, tuple(out)


def step_failed(s: RunnerState, error: str) -> Step:
    """`step` raised: halt this account, not the runner."""
    s, out = halt(s, f"strategy error: {error}")
    return s, (Log("bar", {**asdict(s.bars[-1]), "actions": 0}), *out)


def safety_level(s: RunnerState) -> float:
    """DEC-4c: the higher of start − margin × DLL (when on) and floor + (1 − margin) × room."""
    m = s.setup.safety_margin_pct / 100
    level = s.acct.floor + (1 - m) * (s.session_start - s.session_floor)
    dll = phase_rules(s.setup.rules, s.acct.phase).daily_loss_limit
    return level if dll is None else max(level, s.session_start - m * dll)


def account_status(s: RunnerState) -> dict[str, Any]:
    """One AccountStatus (Plans 21–24)."""
    a, rules, acct = s.setup.armed, s.setup.rules, s.acct
    p, v = phase_rules(rules, acct.phase), view(acct, rules)
    return {
        "account": a.account,
        "mode": a.mode,
        "phase": acct.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": s.halted is not None,
        "halt_kind": s.halt_kind,
        "halt_reason": s.halted,
        "balance": acct.balance,
        "today_pnl": v.today_pnl,
        "limits": {
            "drawdown_remaining": v.drawdown_remaining,
            "drawdown_max": p.max_drawdown.amount,
            "daily_loss_remaining": v.daily_loss_remaining,
            "daily_loss_max": p.daily_loss_limit,
            "max_minis": max_minis(rules, acct),
            "held_minis": abs(s.position.qty) * s.setup.spec.minis,
        },
        "position": asdict(s.position),
        "session": {
            "start": s.session_start,
            "floor": acct.floor,
            "target": getattr(p, "profit_target", None),
            "safety": safety_level(s),
            "equity": [[ts.isoformat(), e] for ts, e in s.equity],
            "fills": [],  # 22.1 records fills
        },
    }


def _payload(a: Action) -> dict[str, Any]:
    return {"type": type(a).__name__, **asdict(a)}


def _halt(s: RunnerState, reason: str, kind: str | None, until: date | None) -> Step:
    if s.halted == reason or (s.halted and s.halt_day is None):
        return s, ()  # an open-ended halt is never replaced or downgraded to a day halt
    payload = {"reason": reason, "kind": kind, "until": until.isoformat() if until else None}
    return replace(s, halted=reason, halt_kind=kind, halt_day=until), (Log("halt", payload),)


def _safety(s: RunnerState) -> Step:
    a = s.acct
    level = safety_level(s)
    if s.halted or not at_or_below(a.balance + a.open_pnl, level):
        return s, ()
    reason = f"safety margin: equity {a.balance + a.open_pnl:.2f} at or below {level:.2f}"
    return _halt(s, reason, "safety", a.day)


def _block(s: RunnerState, a: Action, pending: bool) -> str | None:
    """Why this action may not go out, or None. Flatten and Cancel always may."""
    if not isinstance(a, Bracket):
        return None
    if a.side not in ("long", "short"):
        return f"side must be long or short, got {a.side!r}"
    if s.halted:
        return f"halted: {s.halted}"
    if pending:
        return "a bracket was already accepted this bar"
    if s.position.qty != 0:
        return "already in a position"
    bar, rules = s.bars[-1], s.setup.rules
    # shortcut (as the backtest): compares HH:MM in rules.session.tz with ET
    flat_at = min(rules.session.flat_by, ADDON_FLATTEN)
    if at_or_after(bar.ts + MINUTE, flat_at, rules.session.tz):
        return f"at or after {flat_at} flat-by: no new entries"
    # 21.3 (#77) adds the roll-window block and 21.4 (#78) the stale-data pause here.
    # 22.2 swaps s.acct for the stricter of it and NinjaTrader's figures (DEC-4b).
    return check(s.acct, rules, a, s.setup.spec, entry_price=bar.close, held_minis=0.0)
