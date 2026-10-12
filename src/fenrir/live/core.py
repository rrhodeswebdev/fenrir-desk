"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O or clock.

Per closed bar the shell calls `on_bar`, runs the strategy's `step` on `ctx(state)` in its own child
process (DEC-1), then passes the returned actions to `decide` (or the error to `step_failed`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fenrir.backtest.attempt import ADDON_FLATTEN, MINUTE, WINDOW
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec, in_roll_window
from fenrir.core.sessions import NY, at_or_after, trading_day
from fenrir.live.arming import Armed
from fenrir.rules.account import apply, build_rules, initial_state, phase_rules, view
from fenrir.rules.contracts import max_minis
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, Breach, DayClosed, Phase, at_or_below
from fenrir.strategy import FLAT, Ctx, Position


@dataclass(frozen=True, slots=True)
class Start:
    """NinjaTrader balances in absolute dollars for a sim or live account (DEC-4a)."""

    phase_start: float  # balance when the current phase began (armed.start_balance)
    peak: float  # highest end-of-day balance (end_of_day) or intraday equity (intraday) so far
    balance: float  # balance now


@dataclass(frozen=True, slots=True)
class DayStart:
    """NinjaTrader figures at today's session open, absolute dollars; 21.5 or 22.2 supplies them."""

    balance: float  # balance at the open
    peak: float  # the drawdown peak at the open (as Start.peak), which fixes the open's floor


@dataclass(frozen=True, slots=True)
class Setup:
    armed: Armed
    strategy_hash: str  # file_hash of the strategy file on disk now
    rules: AccountRules  # with_dll already applied
    spec: ContractSpec
    safety_margin_pct: float
    start: Start | None = None  # ignored for dry runs, which start fresh (DEC-4a)
    # sim and live take no new brackets while it is None; ignored for dry runs
    day_start: DayStart | None = None


@dataclass(frozen=True, slots=True)
class RunnerState:
    setup: Setup
    acct: AccountState
    position: Position
    bars: tuple[Bar, ...]  # last <= WINDOW bars, ending with the current bar
    equity: tuple[tuple[datetime, float], ...]  # close-mark equity per bar this trading day
    session_start: float  # equity at session start, relative to phase start
    session_floor: float  # drawdown floor at session start
    last_seen: datetime  # close of the last bar, or the start time
    halted: str | None = None  # no new entries while set
    halt_kind: str | None = None  # "safety" | "breach" | ... (Plans 21–24 AccountStatus)
    halt_day: date | None = None  # halt lifts after this trading day; None = until disarmed
    started: bool = True  # False: start refused, so the figures are not the account's
    stale: str | None = None  # stale-data pause: no new entries until the next bar


@dataclass(frozen=True, slots=True)
class Log:
    kind: str  # a fenrir.live.journal KINDS value
    payload: Mapping[str, Any]


Step = tuple[RunnerState, tuple[Log, ...]]


def start_account(
    rules: AccountRules,
    phase: Phase,
    day: date,
    start: Start | None,
    day_start: DayStart | None = None,
) -> AccountState:
    """State relative to phase start; with NinjaTrader balances the floor trails their peak."""
    fresh = initial_state(rules, phase, day)
    if start is None:
        return fresh
    dd = phase_rules(rules, phase).max_drawdown
    # an intraday peak is never below the balance, whatever was entered at arming
    top = max(start.peak, start.balance) if dd.trailing == "intraday" else start.peak
    peak = top - start.phase_start
    balance = start.balance - start.phase_start
    # without day_start the balance now stands in; brackets are blocked until it arrives
    opened = balance if day_start is None else day_start.balance - start.phase_start
    return replace(
        fresh,
        balance=balance,
        day_start_balance=opened,
        # shortcut: a day that nets exactly $0 since the open is not booked; 21.5 replays the fills
        traded_today=balance != opened,
        peak=peak,
        floor=_floor(rules, phase, peak),
    )


def _floor(rules: AccountRules, phase: Phase, peak: float) -> float:
    """The drawdown floor for a peak relative to phase start, as drawdown.trail sets it."""
    dd = phase_rules(rules, phase).max_drawdown
    level = peak if dd.locks_at is None else min(peak, dd.locks_at)
    return max(-dd.amount, level - dd.amount)


def start(setup: Setup, now: datetime) -> Step:
    a = setup.armed
    begin = None if a.mode == "dry" else setup.start
    day = setup.day_start if begin else None
    acct = start_account(setup.rules, a.phase, trading_day(now), begin, day)
    # without day_start these are the restart's figures; brackets are blocked until it arrives
    if day is not None:  # the open's floor, from the open's figures as the start's
        at_open = Start(begin.phase_start, day.peak, day.balance)
        floor = start_account(setup.rules, a.phase, acct.day, at_open).floor
    else:
        floor = acct.floor
    s = RunnerState(setup, acct, FLAT, (), (), acct.day_start_balance, floor, now)
    if setup.strategy_hash != a.strategy_hash:
        return halt(replace(s, started=False), "strategy file changed since arming; re-arm")
    if a.mode != "dry" and setup.start is None:
        reason = "sim and live need the NinjaTrader start and peak balances (DEC-4a)"
        return halt(replace(s, started=False), reason)
    return s, ()


def halt(s: RunnerState, reason: str) -> Step:
    """Halt until disarmed."""
    return _halt(s, reason, None, None)


def on_bar(s: RunnerState, bar: Bar) -> Step:
    s, out = _roll_day(s, bar)
    # 22.1 applies this bar's fills, then its Mark, here (backtest calling order).
    a = s.acct
    s = replace(
        s,
        bars=(*s.bars, bar)[-WINDOW:],
        equity=(*s.equity, (bar.ts, a.balance + a.open_pnl)),
        last_seen=bar.ts + MINUTE,
        stale=None,
    )
    s, more = _safety(s)
    return s, out + more


def on_tick(s: RunnerState, now: datetime, stale_seconds: int) -> Step:
    """No bar closed for `stale_seconds` in session: pause new entries, with one alert."""
    # the whole window in session, so the reopen after a break or weekend is not "stale"
    window = timedelta(seconds=stale_seconds) + MINUTE
    if s.stale or not (in_session(now) and in_session(now - window)):
        return s, ()
    age = (now - s.last_seen).total_seconds()
    if age <= stale_seconds:
        return s, ()
    reason = f"no {s.setup.spec.series} bar for {age:.0f}s; new entries paused"
    return replace(s, stale=reason), (Log("alert", {"message": reason}),)


def in_session(now: datetime) -> bool:
    """CME Globex: Sunday 18:00 to Friday 17:00 ET, closed 17:00–18:00 each day."""
    # shortcut: CME holidays count as in session (a stale pause); add a calendar if noisy
    t = now.astimezone(NY)
    hm = (t.hour, t.minute)
    match t.weekday():
        case 5:
            return False
        case 6:
            return hm >= (18, 0)
        case 4:
            return hm < (17, 0)
    return not ((17, 0) <= hm < (18, 0))


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
    decided: list[tuple[Action, str | None]] = []
    pending = None  # index of a bracket accepted earlier this bar, as the backtest's `pending`
    for a in actions:
        reason = _block(s, a, pending is not None)
        if isinstance(a, (Flatten, Cancel)) and pending is not None:
            # the backtest drops that bracket before it is ever placed
            decided[pending] = (decided[pending][0], f"replaced by a {type(a).__name__} this bar")
            pending = None
        elif isinstance(a, Bracket) and reason is None:
            pending = len(decided)
        decided.append((a, reason))
    mode = s.setup.armed.mode
    return s, (
        Log("bar", {**asdict(s.bars[-1]), "actions": len(actions)}),
        *(
            Log("rejection", {"action": _payload(a), "reason": r})
            if r
            else Log("action", {**_payload(a), "mode": mode, "sent": False})
            for a, r in decided
        ),
    )


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
    head = {
        "account": a.account,
        "mode": a.mode,
        "phase": acct.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": s.halted is not None or s.stale is not None,
        "halt_kind": s.halt_kind if s.halted else "stale" if s.stale else None,
        "halt_reason": s.halted or s.stale,
    }
    if not s.started:
        return head | dict.fromkeys(("balance", "today_pnl", "limits", "position", "session"))
    return head | {
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


def _roll_day(s: RunnerState, bar: Bar) -> Step:
    """Close the last trading day (one DayClosed, as the backtest) and open the bar's day."""
    day = trading_day(bar.ts)
    if day <= s.acct.day:
        return s, ()
    out: tuple[Log, ...] = ()
    acct = apply(build_rules(s.setup.rules, s.acct.phase), s.acct, DayClosed(s.acct.day, bar.ts))
    if isinstance(acct, Breach):
        s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
        acct = s.acct  # failed until disarmed: its figures stay as they were
    acct = replace(acct, day=day)
    # the new day opens where the last closed: nothing trades in the 17:00–18:00 ET break
    s = replace(
        s, acct=acct, equity=(), session_start=acct.day_start_balance, session_floor=acct.floor
    )
    if s.halt_day is not None and day > s.halt_day:
        out += (Log("alert", {"message": f"halt lifted: {s.halted}"}),)
        s = replace(s, halted=None, halt_kind=None, halt_day=None)
    return s, out


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
    if s.stale:
        return f"paused: {s.stale}"
    if pending:
        return "a bracket was already accepted this bar"
    if s.position.qty != 0:
        return "already in a position"
    bar, rules = s.bars[-1], s.setup.rules
    # shortcut (as the backtest): compares HH:MM in rules.session.tz with ET
    flat_at = min(rules.session.flat_by, ADDON_FLATTEN)
    if at_or_after(bar.ts + MINUTE, flat_at, rules.session.tz):
        return f"at or after {flat_at} flat-by: no new entries"
    if s.setup.armed.mode != "dry" and s.setup.day_start is None:
        return "today's opening balance unknown"
    if in_roll_window(trading_day(bar.ts)):
        return "roll window: no new entries"  # the backtest loads no bars on these days
    # 22.2 swaps s.acct for the stricter of it and NinjaTrader's figures (DEC-4b).
    return check(s.acct, rules, a, s.setup.spec, entry_price=bar.close, held_minis=0.0)
