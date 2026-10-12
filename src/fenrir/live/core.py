"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O or clock.

Per closed bar the shell calls `on_bar`, runs the strategy's `step` on `ctx(state)` in its own child
process (DEC-1), then passes the returned actions to `decide` (or the error to `step_failed`).
Effects are `Log`s to journal and, for sim and live, `Send`s for the shell to post to the add-on;
the add-on's fills come back through `on_fill`, and a failed post through `send_failed`.
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fenrir.backtest.attempt import ADDON_FLATTEN, MINUTE, WINDOW
from fenrir.backtest.fills import OpenPosition, best_open_pnl, gross_pnl, worst_open_pnl
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec, in_roll_window
from fenrir.core.sessions import NY, at_or_after, trading_day
from fenrir.live.arming import Armed
from fenrir.rules.account import apply, build_rules, initial_state, phase_rules, view
from fenrir.rules.contracts import max_minis
from fenrir.rules.ledger import book
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import (
    AccountState,
    Breach,
    DayClosed,
    Event,
    Fill,
    Mark,
    Phase,
    TradeClosed,
    at_or_below,
)
from fenrir.strategy import FLAT, Ctx, Position

# shortcut: fixed band for LSE-close-vs-entry-fill drift; make it a setting if sim shows otherwise
DRIFT_TICKS = 4


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
class Working:
    """A bracket sent to the add-on whose entry has not filled yet."""

    client_id: str
    bracket: Bracket
    close: float  # the LSE close it was checked at, for the drift check


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
    working: Working | None = None  # no new entries while an entry is working
    exiting: str | None = None  # client_id of the Flatten out (runner or step), until flat
    drift: str | None = None  # entry-fill drift pause: no new entries until resolved (22.3)
    fills: tuple[datetime, ...] = ()  # this trading day's fill times
    opened: datetime | None = None  # first fill of the open position
    opened_balance: float = 0.0  # acct.balance before that fill, for TradeClosed
    held: tuple[Position, float] | None = None  # position and balance before this bar's exit


@dataclass(frozen=True, slots=True)
class Log:
    kind: str  # a fenrir.live.journal KINDS value
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Send:
    """Post `action` to the add-on under `client_id` (sim and live only)."""

    action: Action
    client_id: str


Step = tuple[RunnerState, tuple[Log | Send, ...]]


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
    s, out = _roll_day(s, bar.ts)
    # backtest calling order: the entry fill, then the Mark of the position held, then the exit
    if s.held:  # an exit filled in this bar: mark what was held, at the balance before the exit
        held, balance = s.held
        m, shift = _mark(held, bar, s.setup.spec), balance - s.acct.balance
        s, more = _event(s, Mark(bar.ts, m.worst_open_pnl + shift, m.best_open_pnl + shift, 0.0))
        out += more
    s, more = _event(replace(s, held=None), _mark(s.position, bar, s.setup.spec))
    out += more
    if s.acct.halted_today:
        s, more = _halt(s, "daily loss limit hit: no new entries today", "safety", s.acct.day)
        out += more
    a = s.acct
    s = replace(
        s,
        bars=(*s.bars, bar)[-WINDOW:],
        equity=(*s.equity, (bar.ts, a.balance + a.open_pnl)),
        last_seen=bar.ts + MINUTE,
        stale=None,
    )
    s, more = _safety(s)
    out += more
    s, more = _exit(s, bar)
    return s, out + more


def on_fill(s: RunnerState, fill: Mapping[str, Any]) -> Step:
    """Book one add-on `fill` message (validated by fenrir.bridge.client) on this account's
    instrument; the shell routes fills by account and instrument."""
    ts = datetime.fromisoformat(fill["ts"])
    qty, price = fill["qty"], fill["price"]
    s, out = _roll_day(s, ts)
    out += (Log("fill", dict(fill)),)
    if qty == 0:
        return s, (*out, Log("alert", {"message": "ignored a fill with qty 0"}))
    pos, w, spec = s.position, s.working, s.setup.spec
    rest = pos.qty + qty
    flipped = rest != 0 and pos.qty != 0 and (rest > 0) != (pos.qty > 0)
    # an entry fill carries its bracket's client_id
    mine = pos.qty == 0 and w is not None and fill["client_id"] == w.client_id
    opening = pos.qty == 0 or (pos.qty > 0) == (qty > 0)
    if opening:  # or adding
        entry = (abs(pos.qty) * (pos.entry or 0.0) + abs(qty) * price) / abs(rest)
        b = w.bracket if mine else pos
        position, realized = Position(rest, entry, b.stop, b.target), 0.0
    else:
        closed = min(abs(qty), abs(pos.qty)) * (1 if pos.qty > 0 else -1)
        realized = gross_pnl(closed, pos.entry, price, spec.point_value)
        if rest == 0:
            position = FLAT
        elif (rest > 0) == (pos.qty > 0):
            position = replace(pos, qty=rest)
        else:  # through zero: the rest is a new position with no known bracket
            position = Position(rest, price, None, None)
    # the stricter of NinjaTrader's commission and the rules' (which the backtest charges)
    per_side = getattr(s.setup.rules.costs.commission_per_side, spec.symbol)
    commission = max(fill["commission"], per_side * abs(qty), 0.0)
    before, today = s.acct.balance, s.acct.day
    s = replace(
        s,
        position=position,
        fills=(*s.fills, ts),
        working=None if mine else w,
        # an over-close that reversed the position ends that Flatten: the reversal gets its own
        exiting=None if rest == 0 or flipped else s.exiting,
        held=s.held or (None if opening else (pos, before)),
    )
    s, more = _event(s, Fill(ts, qty, price, commission, realized))
    out += more
    # a late fill from a closed day books into today, or the next bar would close that day again
    s = replace(s, acct=replace(s.acct, day=max(s.acct.day, today)))
    if rest == 0 or flipped:  # the open trade closed
        same_bar = s.opened is not None and _minute(s.opened) == _minute(ts)
        trade = TradeClosed(s.opened or ts, ts, s.acct.balance - s.opened_balance, same_bar)
        s, more = _event(s, trade)
        out += more
    if pos.qty == 0 or flipped:  # a new trade opened
        s = replace(s, opened=ts, opened_balance=before if pos.qty == 0 else s.acct.balance)
    if mine and s.drift is None:
        ticks = abs(price - w.close) / spec.tick
        if round(ticks, 6) > DRIFT_TICKS:
            reason = (
                f"entry fill {price} is {ticks:g} ticks from the LSE close {w.close};"
                " new entries paused until resolved"
            )
            s = replace(s, drift=reason)
            out += (Log("alert", {"message": reason}),)
    if s.position.qty and s.position.stop is None and not s.exiting:  # no broker-side stop known
        s, more = _flatten(s, ts, "f", f"position {s.position.qty} has no known stop")
        out += more
    return s, out


def send_failed(s: RunnerState, client_id: str, error: str) -> Step:
    """The add-on answered a `Send` with `accepted: false` (not `duplicate`): nothing landed.

    Never for a transport error or timeout: the order may have landed, so its entry stays
    working (no second bracket) until a fill, the day roll or reconciliation (22.3)."""
    s = replace(
        s,
        working=None if s.working and s.working.client_id == client_id else s.working,
        exiting=None if s.exiting == client_id else s.exiting,  # the next bar sends another
    )
    return s, (Log("alert", {"message": f"send {client_id} failed: {error}"}),)


def client_id(account: str, ts: datetime, suffix: str, action: Action) -> str:
    """24 chars and deterministic, so a retry or a restart that re-decides the bar is deduped by
    the add-on. `ts` is the bar's open (a fill's time for "f"); `suffix` the action's index, or
    "x" / "f" for the runner's own Flatten on a bar / a fill. The action's type is hashed in, so a
    re-decided bar whose list changed never gives a Flatten a Bracket's id (a false duplicate)."""
    key = f"{account}\0{suffix}\0{type(action).__name__}"
    tag = hashlib.sha256(key.encode()).hexdigest()[:13]
    return f"{ts.astimezone(UTC):%y%m%d%H%M}-{tag}"


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


def decide(s: RunnerState, actions: Sequence[Action], now: datetime, stale_seconds: int) -> Step:
    """Pre-trade check every action `step` returned for the current bar; sim and live send each
    accepted one (`Send` after its "action" log), dry runs only log it.

    `actions` are Action values: the shell parses the child's JSON into them (DEC-1). `now` is
    when they go out: a bar that closed over `stale_seconds` ago (late or replayed) takes no entry.
    """
    age = (now - s.last_seen).total_seconds()
    late = f"bar closed {age:.0f}s ago (over {stale_seconds}s): no entry at its close"
    old = late if age > stale_seconds else None
    decided: list[tuple[Action, str | None]] = []
    pending = None  # index of a bracket accepted earlier this bar, as the backtest's `pending`
    for a in actions:
        reason = _block(s, a, pending is not None, old)
        if isinstance(a, (Flatten, Cancel)) and pending is not None:
            # the backtest drops that bracket before it is ever placed
            decided[pending] = (decided[pending][0], f"replaced by a {type(a).__name__} this bar")
            pending = None
        elif isinstance(a, Bracket) and reason is None:
            pending = len(decided)
        decided.append((a, reason))
    bar, mode = s.bars[-1], s.setup.armed.mode
    out: tuple[Log | Send, ...] = (Log("bar", {**asdict(bar), "actions": len(actions)}),)
    for i, (a, r) in enumerate(decided):
        if r:
            out += (Log("rejection", {"action": _payload(a), "reason": r}),)
            continue
        if isinstance(a, Flatten) and (s.position.qty == 0 or s.exiting):
            # one /flatten at a time, and none while flat (the backtest's Flatten is a no-op then)
            why = f"Flatten {s.exiting} already working" if s.exiting else "flat: nothing to close"
            out += (Log("action", {**_payload(a), "mode": mode, "sent": False, "reason": why}),)
            continue
        cid = client_id(s.setup.armed.account, bar.ts, str(i), a)
        out += _send(s, a, cid)
        if mode != "dry" and isinstance(a, Bracket):
            s = replace(s, working=Working(cid, a, bar.close))
        elif mode != "dry" and isinstance(a, Flatten):
            s = replace(s, exiting=cid)
    return s, out


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
    pause = "stale" if s.stale else "drift" if s.drift else None
    head = {
        "account": a.account,
        "mode": a.mode,
        "phase": acct.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": bool(s.halted or s.stale or s.drift),
        "halt_kind": s.halt_kind if s.halted else pause,
        "halt_reason": s.halted or s.stale or s.drift,
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
            "fills": [ts.isoformat() for ts in s.fills],
        },
    }


def _payload(a: Action) -> dict[str, Any]:
    return {"type": type(a).__name__, **asdict(a)}


def _send(s: RunnerState, a: Action, cid: str, **why: str) -> tuple[Log | Send, ...]:
    mode = s.setup.armed.mode
    # a Cancel only drops a bracket from the same bar (decide), as in the backtest: step cannot
    # know NinjaTrader's order ids, so one is never sent
    if mode == "dry" or isinstance(a, Cancel):
        return (Log("action", {**_payload(a), "mode": mode, "sent": False, **why}),)
    log = Log("action", {**_payload(a), "mode": mode, "sent": True, "client_id": cid, **why})
    return log, Send(a, cid)


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _event(s: RunnerState, event: Event) -> Step:
    """Apply one event through the phase's rules; a Breach halts until disarmed."""
    acct = apply(build_rules(s.setup.rules, s.acct.phase), s.acct, event)
    if not isinstance(acct, Breach):
        return replace(s, acct=acct), ()
    s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
    return replace(s, acct=book(s.acct, event)), out  # failed: keep the books, not the rules


def _mark(p: Position, bar: Bar, spec: ContractSpec) -> Mark:
    """The backtest's Mark (`attempt._mark`); flat, a zero Mark checks the realized P&L (the
    backtest skips it on a bar with no fill, where it changes nothing)."""
    pv = spec.point_value
    if p.qty == 0:
        return Mark(bar.ts, 0.0, 0.0, 0.0)
    far = math.inf if p.qty > 0 else -math.inf  # an unknown stop or target caps nothing
    pos = OpenPosition(
        p.qty,
        p.entry,
        -far if p.stop is None else p.stop,
        far if p.target is None else p.target,
        bar.ts,
    )
    close = gross_pnl(p.qty, p.entry, bar.close, pv)
    return Mark(bar.ts, worst_open_pnl(pos, bar, pv), best_open_pnl(pos, bar, pv), close)


def _late(s: RunnerState, bar: Bar) -> str | None:
    """The flat-by time when this bar closes at or after it (the backtest's `_late`), else None."""
    rules = s.setup.rules
    # shortcut (as the backtest): compares HH:MM in rules.session.tz with ET
    flat_at = min(rules.session.flat_by, ADDON_FLATTEN)
    return flat_at if at_or_after(bar.ts + MINUTE, flat_at, rules.session.tz) else None


def _exit(s: RunnerState, bar: Bar) -> Step:
    """Flatten once while holding past flat-by (the backtest's `_flat_time`), after the daily loss
    limit (its `_dll_exit`), after a breach, or with no known stop; the add-on also flattens at
    16:40 on its own."""
    if s.position.qty == 0 or s.exiting:
        return s, ()
    if late := _late(s, bar):
        why = f"flat-by {late}"
    elif s.acct.halted_today:
        why = "daily loss limit hit"
    elif s.halt_kind == "breach" and s.halted:
        why = s.halted
    elif s.position.stop is None:
        why = f"position {s.position.qty} has no known stop"
    else:
        return s, ()
    return _flatten(s, bar.ts, "x", why)


def _flatten(s: RunnerState, ts: datetime, suffix: str, why: str) -> Step:
    """The runner's own Flatten; no other goes out until flat (`exiting`)."""
    cid = client_id(s.setup.armed.account, ts, suffix, Flatten())
    return replace(s, exiting=cid), _send(s, Flatten(), cid, reason=why)


def _halt(s: RunnerState, reason: str, kind: str | None, until: date | None) -> Step:
    if s.halted == reason or (s.halted and s.halt_day is None):
        return s, ()  # an open-ended halt is never replaced or downgraded to a day halt
    payload = {"reason": reason, "kind": kind, "until": until.isoformat() if until else None}
    return replace(s, halted=reason, halt_kind=kind, halt_day=until), (Log("halt", payload),)


def _roll_day(s: RunnerState, ts: datetime) -> Step:
    """Close the last trading day (one DayClosed, as the backtest) and open the day of `ts`: the
    first bar or fill of the new day, so a day closes after its own exit fills."""
    day = trading_day(ts)
    if day <= s.acct.day:
        return s, ()
    out: tuple[Log | Send, ...] = ()
    acct = apply(build_rules(s.setup.rules, s.acct.phase), s.acct, DayClosed(s.acct.day, ts))
    if isinstance(acct, Breach):
        s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
        acct = s.acct  # failed until disarmed: its figures stay as they were
    acct = replace(acct, day=day)
    # the new day opens where the last closed: nothing trades in the 17:00–18:00 ET break, and
    # the add-on cancelled working orders at 16:40
    s = replace(
        s,
        acct=acct,
        equity=(),
        fills=(),
        working=None,
        exiting=None,
        held=None,
        session_start=acct.day_start_balance,
        session_floor=acct.floor,
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


def _block(s: RunnerState, a: Action, pending: bool, old: str | None) -> str | None:
    """Why this action may not go out, or None. Flatten and Cancel always may."""
    if not isinstance(a, Bracket):
        return None
    if a.side not in ("long", "short"):
        return f"side must be long or short, got {a.side!r}"
    if s.halted:
        return f"halted: {s.halted}"
    if s.stale or s.drift:
        return f"paused: {s.stale or s.drift}"
    if old:  # a late or replayed bar: never trade at its old close
        return old
    if pending:
        return "a bracket was already accepted this bar"
    if s.working:
        return f"entry {s.working.client_id} still working"
    if s.position.qty != 0:
        return "already in a position"
    bar, rules = s.bars[-1], s.setup.rules
    if flat_at := _late(s, bar):
        return f"at or after {flat_at} flat-by: no new entries"
    if s.setup.armed.mode != "dry" and s.setup.day_start is None:
        return "today's opening balance unknown"
    if in_roll_window(trading_day(bar.ts)):
        return "roll window: no new entries"  # the backtest loads no bars on these days
    # 22.2 swaps s.acct for the stricter of it and NinjaTrader's figures (DEC-4b).
    return check(s.acct, rules, a, s.setup.spec, entry_price=bar.close, held_minis=0.0)
