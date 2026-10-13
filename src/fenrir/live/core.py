"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O or clock.

Per closed bar the shell calls `on_bar`, runs the strategy's `step` on `ctx(state)` in its own child
process (DEC-1), then passes the returned actions to `decide` (or the error to `step_failed`).
Effects are `Log`s to journal and, for sim and live, `Send`s for the shell to post to the add-on;
the add-on's fills come back through `on_fill`, and a failed post through `send_failed`. Sim and
live take no new entries until NinjaTrader's book is reconciled (`on_reconcile`), on start and after
every lost link (`on_disconnect`); after a restart `replay` rebuilds today's book from the journal.
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
from fenrir.bridge.reconcile import ExpectedBook, NtOrder, NtPosition, reconcile
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
CONFIRM_BALANCES = (
    "confirm balances: an end-of-day session close passed while the runner was down;"
    " re-arm with the confirmed peak balance"
)
UNRECONCILED = "not reconciled with NinjaTrader yet: no new entries"
LINK_LOST = "NinjaTrader link lost: no new entries until reconciled"


@dataclass(frozen=True, slots=True)
class Start:
    """NinjaTrader balances in absolute dollars for a sim or live account (DEC-4a)."""

    phase_start: float  # balance when the current phase began (armed.start_balance)
    peak: float  # highest end-of-day balance (end_of_day) or intraday equity (intraday) so far
    balance: float  # balance now


@dataclass(frozen=True, slots=True)
class DayStart:
    """NinjaTrader figures at today's session open, absolute $; 21.5 or the hub supplies them."""

    balance: float  # balance at the open
    peak: float  # the drawdown peak at the open (as Start.peak), which fixes the open's floor


@dataclass(frozen=True, slots=True)
class NtAccount:
    """NinjaTrader's latest `account` message for a sim or live account, absolute dollars."""

    balance: float
    realized_pnl_today: float
    unrealized_pnl: float


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
    drift: str | None = None  # entry-fill drift pause: no new entries until resolved
    unreconciled: str | None = None  # sim/live: no new entries until NinjaTrader's book matches
    mismatch: str | None = None  # reconcile pause: no new entries until the user resolves it
    fills: tuple[datetime, ...] = ()  # this trading day's fill times
    opened: datetime | None = None  # first fill of the open position
    opened_balance: float = 0.0  # acct.balance before that fill, for TradeClosed
    held: tuple[Position, float] | None = None  # position and balance before this bar's exit
    nt: NtAccount | None = None  # for the stricter-of checks (DEC-4b); never for dry runs


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


def start(
    setup: Setup,
    now: datetime,
    halts: Sequence[Mapping[str, Any]] = (),
    peaks: Sequence[Mapping[str, Any]] = (),
) -> Step:
    """`halts`: the "halt" payloads journalled since `armed.armed_at`, oldest first; the ones
    still in force are restored (already journalled, so not again). `peaks`: the "peak" payloads
    journalled since then; the highest of them and `start.peak` is the peak (DEC-4a)."""
    a = setup.armed
    begin = None if a.mode == "dry" else setup.start
    if begin is not None:
        begin = replace(begin, peak=max([begin.peak, *(p["peak"] for p in peaks)]))
    day = setup.day_start if begin else None
    acct = start_account(setup.rules, a.phase, trading_day(now), begin, day)
    # an intraday peak starts at least at the balance: journal that raise too
    raised = begin is not None and acct.peak > begin.peak - begin.phase_start
    out = (_peak_log(setup, acct, now),) if raised else ()
    # without day_start these are the restart's figures; brackets are blocked until it arrives
    if day is not None:  # the open's floor, from the open's figures as the start's
        at_open = Start(begin.phase_start, day.peak, day.balance)
        floor = start_account(setup.rules, a.phase, acct.day, at_open).floor
    else:
        floor = acct.floor
    waiting = None if a.mode == "dry" else UNRECONCILED
    s = RunnerState(
        setup, acct, FLAT, (), (), acct.day_start_balance, floor, now, unreconciled=waiting
    )
    for h in halts:  # replayed in order, so open-ended halts and breaches win as they did live
        match h["kind"]:  # first the pauses that only `resolve` lifts
            case "reconcile":
                s = replace(s, mismatch=h["reason"])
            case "drift":
                s = replace(s, drift=h["reason"])
            case "resolve":
                s = replace(s, mismatch=None, drift=None)
            case _:
                until = h["until"] and date.fromisoformat(h["until"])
                if until is None or until >= acct.day:  # a day halt from an earlier day has lifted
                    s, _ = _halt(s, h["reason"], h["kind"], until)
    if setup.strategy_hash != a.strategy_hash:
        s, more = halt(replace(s, started=False), "strategy file changed since arming; re-arm")
        return s, out + more
    if a.mode != "dry" and setup.start is None:
        reason = "sim and live need the NinjaTrader start and peak balances (DEC-4a)"
        return halt(replace(s, started=False), reason)
    trailing = phase_rules(setup.rules, a.phase).max_drawdown.trailing
    if begin and trailing == "end_of_day" and _missed_close(a.armed_at, peaks, acct.day):
        s, more = halt(s, CONFIRM_BALANCES)
        return s, out + more
    return s, out


def _missed_close(armed_at: datetime, peaks: Sequence[Mapping[str, Any]], today: date) -> bool:
    """end_of_day: each day roll (and only a roll) journals a "peak" row stamped with its bar or
    fill. A session that opened after the last roll (or the arming), up to `today`, means a close
    the restarted runner never raises at: its balance may have moved since (DEC-4a)."""
    rolled = trading_day(max([armed_at, *(datetime.fromisoformat(p["ts"]) for p in peaks)]))
    # trading days with a session (Mon-Fri; holidays count) open at 18:00 ET the evening before
    days = (rolled + timedelta(days=n) for n in range(1, (today - rolled).days + 1))
    return any(d.weekday() < 5 for d in days)


def halt(s: RunnerState, reason: str) -> Step:
    """Halt until disarmed."""
    return _halt(s, reason, None, None)


def on_bar(s: RunnerState, bar: Bar) -> Step:
    s, out = _roll_day(s, bar.ts)  # journals its own "peak" row
    was = s
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
    return s, out + more + _peaked(was, s, bar.ts)


def on_fill(s: RunnerState, fill: Mapping[str, Any]) -> Step:
    """Book one add-on `fill` message (validated by fenrir.bridge.client) on this account's
    instrument; the shell routes fills by account and instrument."""
    ts = datetime.fromisoformat(fill["ts"])
    qty, price = fill["qty"], fill["price"]
    s, out = _roll_day(s, ts)  # journals its own "peak" row; a Fill never moves the peak
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
            s = replace(s, drift=reason)  # a "halt" row, so a restart restores it
            out += (Log("halt", {"reason": reason, "kind": "drift", "until": None}),)
    if s.position.qty and s.position.stop is None and not s.exiting:  # no broker-side stop known
        # "f<n>": today's n-th fill, so each fill's Flatten has its own id, the same on a replay
        s, more = _stopless(s, ts, f"f{len(s.fills)}")
        out += more
    return s, out


def on_account(s: RunnerState, account: Mapping[str, Any], now: datetime) -> Step:
    """One add-on `account` message for this account (validated by fenrir.bridge.client); dry
    runs ignore them. A sim/live mismatch halts until disarmed; otherwise the figures feed the
    stricter-of checks (DEC-4b), and an intraday peak follows NinjaTrader's equity (DEC-4a)."""
    mode = s.setup.armed.mode
    if mode == "dry":
        return s, ()
    if account["is_sim"] != (mode == "sim"):
        theirs = "sim" if account["is_sim"] else "live"
        return halt(s, f"mode mismatch: armed {mode}, NinjaTrader reports a {theirs} account")
    if s.setup.start is None:  # could not start: no phase start to set its figures against
        return s, ()
    nt = NtAccount(account["balance"], account["realized_pnl_today"], account["unrealized_pnl"])
    was, s = s, replace(s, nt=nt)
    if phase_rules(s.setup.rules, s.acct.phase).max_drawdown.trailing == "intraday":
        s = replace(s, acct=_raised(s.setup, s.acct, nt.balance + nt.unrealized_pnl))
    return s, _peaked(was, s, now)


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


def on_disconnect(s: RunnerState, error: str) -> Step:
    """The add-on stream dropped or missed its heartbeats: no new entries until reconciled, with
    one "alert" per outage. Positions keep their broker-side stop and target (the add-on's
    `flatten_on_disconnect_seconds` may flatten them: DEC-5). Dry runs ignore it."""
    if s.setup.armed.mode == "dry" or s.unreconciled == LINK_LOST:
        return s, ()
    return replace(s, unreconciled=LINK_LOST), (
        Log("alert", {"message": f"{LINK_LOST} ({error})"}),
    )


def on_reconcile(
    s: RunnerState,
    instrument: str,
    positions: Sequence[NtPosition],
    orders: Sequence[NtOrder],
    now: datetime,
) -> Step:
    """NinjaTrader's `/positions` and working `/orders` for this account (`instrument`: its name
    for the armed contract), on start, after every reconnect and on `resolve`; dry runs ignore it.

    NinjaTrader is the source of truth: the runner takes its position on the instrument, with
    the stop and target that cover it, keeps its working entry only while NinjaTrader still works
    it, and forgets a Flatten in flight (`/flatten` never over-closes). Every reconcile lifts
    the `unreconciled` pause; a mismatch pauses entries until the user resolves it, and a position
    with no covering stop is flattened at once as well."""
    a, w = s.setup.armed, s.working
    if a.mode == "dry":
        return s, ()
    # the sent entry, still working: not a stray; any other was filled, rejected or cancelled
    entry = [o for o in orders if w and o.client_id == w.client_id and o.role == "entry"]
    rest = [o for o in orders if o not in entry]
    found = reconcile(ExpectedBook(a.account, instrument, s.position.qty), positions, rest)
    mine = [p for p in positions if p.account == a.account and p.instrument == instrument]
    qty = sum(p.qty for p in mine)
    avg = qty and sum(p.qty * p.avg_price for p in mine) / qty
    on = [o for o in rest if o.instrument == instrument]
    # NinjaTrader just reported the book: the position is verified and an outage is over
    s = replace(s, working=w if entry else None, exiting=None, unreconciled=None)
    s = _adopt(s, qty, avg, on, now)
    if not found:
        return s, ()
    detail = "; ".join(f"{m.kind}: {m.detail}" for m in found)
    s, out = _mismatched(s, f"NinjaTrader mismatch: {detail}; fix it there, then resolve")
    if s.position.qty and s.position.stop is None:
        # each reconcile is a fresh read of NinjaTrader: its own id, even twice in one minute
        s, more = _stopless(s, now, f"r{now.timestamp()}")
        out += more
    return s, out


def resolve(
    s: RunnerState,
    instrument: str,
    positions: Sequence[NtPosition],
    orders: Sequence[NtOrder],
    now: datetime,
) -> Step:
    """The user fixed NinjaTrader: lift the reconcile and drift pauses, with a "halt" row of kind
    "resolve" so a restart keeps them lifted, then reconcile with NinjaTrader's book again (a
    mismatch left pauses again). Never lifts a halt: a breach or any other halt stays."""
    lifted = [r for r in (s.mismatch, s.drift) if r]
    lift = {"reason": "resolved: " + "; ".join(lifted), "kind": "resolve", "until": None}
    out = (Log("halt", lift),) if lifted else ()
    s, more = on_reconcile(
        replace(s, mismatch=None, drift=None), instrument, positions, orders, now
    )
    return s, out + more


def replay(s: RunnerState, rows: Sequence[tuple[str, Mapping[str, Any]]]) -> RunnerState:
    """After a restart, `s` from `start`: rebuild today's book from today's journal rows since
    `armed_at` as (kind, payload), oldest first. Sent Brackets and Flattens ("action") become the
    working entry and the Flatten in flight, and the "fill" rows rebuild the position through
    `on_fill`. The balance is the lower of today's opening balance plus the replayed fills (with
    `day_start`) and NinjaTrader's at the restart; without `day_start` it stays NinjaTrader's,
    which already holds today's fills. No effects: every row was journalled when it happened.
    The drift pause stays as `start` restored it from the "halt" rows (a resolve may have lifted
    it), and sim and live stay `unreconciled` until `on_reconcile`."""
    r = s
    if s.setup.day_start is not None:
        r = replace(s, acct=replace(s.acct, balance=s.acct.day_start_balance, traded_today=False))
    close = math.nan  # the last "bar" row's close: a Bracket was checked at it
    for kind, p in rows:
        if kind == "bar":
            close = p["close"]
        elif kind == "action" and p["sent"] and p["type"] == "Bracket":
            b = Bracket(p["side"], p["qty"], p["stop"], p["target"])
            r = replace(r, working=Working(p["client_id"], b, close))
        elif kind == "action" and p["sent"] and p["type"] == "Flatten":
            r = replace(r, exiting=p["client_id"])
        elif kind == "fill":
            r, _ = on_fill(r, p)
    # NinjaTrader's balance at the restart holds every fill, also those made while the runner was
    # down and never journalled: never loosen it. Without day_start it already holds today's.
    booked, a = r.acct.balance, s.acct
    if s.setup.day_start is None:
        acct = replace(r.acct, balance=a.balance, traded_today=a.traded_today)
    else:
        traded = r.acct.traded_today or a.traded_today
        acct = replace(r.acct, balance=min(booked, a.balance), traded_today=traded)
    shift = acct.balance - booked  # the open trade's P&L counts from the balance kept
    # an exit's `held` marks its own bar only, which is gone
    return replace(r, acct=acct, opened_balance=r.opened_balance + shift, drift=s.drift, held=None)


def client_id(account: str, ts: datetime, suffix: str, action: Action) -> str:
    """24 chars and deterministic, so a retry or a restart that re-decides the bar is deduped by
    the add-on. `ts` is the bar's open (a fill's time for "f<n>"); `suffix` the action's index, or
    "x" / "f<n>" (today's n-th fill) for the runner's own Flatten on a bar / a fill. The action's
    type is hashed in, so a re-decided bar whose list changed never gives a Flatten a Bracket's id
    (a false duplicate)."""
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
    p, v = phase_rules(rules, acct.phase), view(_strict(s), rules)
    pause = "reconcile" if s.mismatch else "stale" if s.stale else "drift" if s.drift else None
    head = {
        "account": a.account,
        "mode": a.mode,
        "phase": acct.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": bool(s.halted or s.mismatch or s.stale or s.drift),
        "halt_kind": s.halt_kind if s.halted else pause,
        # the brief unreconciled pause shows only here
        "halt_reason": s.halted or s.mismatch or s.stale or s.drift or s.unreconciled,
    }
    if not s.started:
        keys = ("balance", "nt_balance", "today_pnl", "limits", "position", "session")
        return head | dict.fromkeys(keys)
    return head | {
        "balance": acct.balance,  # relative to phase start, as every figure here
        "nt_balance": None if s.nt is None else s.nt.balance,  # NinjaTrader's, absolute
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
    acct, unverified = _rules(s, event)
    if not isinstance(acct, Breach):
        return _mismatched(replace(s, acct=acct), unverified)
    s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
    return replace(s, acct=book(s.acct, event)), out  # failed: keep the books, not the rules


def _rules(s: RunnerState, event: Event) -> tuple[AccountState | Breach, str | None]:
    """`event` through the phase's rules, and the reconcile pause it raises instead of a flat_by
    breach: while unreconciled the position is unverified (an exit fill missed while the link was
    down; the add-on flattens at 16:40 on its own), so a Mark or day close applies as if flat."""
    rules = build_rules(s.setup.rules, s.acct.phase)
    acct = apply(rules, s.acct, event)
    unverified = isinstance(acct, Breach) and acct.rule == "flat_by" and s.unreconciled is not None
    if not unverified:
        return acct, None
    flat = apply(rules, replace(s.acct, position=0), event)
    if isinstance(flat, Breach):  # drawdown, or a Fill: NinjaTrader's own report of a position
        return flat, None
    why = f"unverified position: {acct.reason}; reconcile with NinjaTrader, then resolve"
    return replace(flat, position=s.acct.position), why


def _mismatched(s: RunnerState, reason: str | None) -> Step:
    """Pause entries until resolved, journalled as a "halt" row of kind "reconcile" (restored on
    restart) each time the reason changes."""
    if reason is None or s.mismatch == reason:
        return s, ()
    return replace(s, mismatch=reason), (
        Log("halt", {"reason": reason, "kind": "reconcile", "until": None}),
    )


def _adopt(
    s: RunnerState, qty: int, avg: float, orders: Sequence[NtOrder], now: datetime
) -> RunnerState:
    """NinjaTrader's `qty` at `avg` on the armed contract, with the stop and target prices that
    cover it (None when they do not); the runner's own entry and trade start stay while the qty
    and side match. Fills missed meanwhile are not booked: NinjaTrader's figures still count."""
    p, acct = s.position, s.acct
    if qty == 0:
        return replace(s, position=FLAT, acct=replace(acct, position=0, open_pnl=0.0), opened=None)
    entry = p.entry if qty == p.qty else avg
    a = s.setup.armed.account
    stop, target = (_cover(a, orders, qty, entry, role) for role in ("stop", "target"))
    position = Position(qty, entry, stop, target)
    if p.qty and (p.qty > 0) == (qty > 0):
        return replace(s, position=position, acct=replace(acct, position=qty))
    return replace(
        s,
        position=position,
        acct=replace(acct, position=qty),
        opened=now,
        opened_balance=acct.balance,
    )


def _cover(
    account: str, orders: Sequence[NtOrder], qty: int, entry: float, role: str
) -> float | None:
    """The price of the `role` orders that close exactly `qty` (as `reconcile` counts them), the
    furthest from `entry` when there are several; None when they do not cover it."""
    closing = "sell" if qty > 0 else "buy"
    mine = [
        o
        for o in orders
        if o.account == account and o.role == role and o.action == closing and o.qty > 0
    ]
    if sum(o.qty for o in mine) != abs(qty):
        return None
    return max((o.price for o in mine), key=lambda price: abs(price - entry))


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
    elif s.position.stop is None:  # its flatten failed (send_failed): send it again
        return _stopless(s, bar.ts, "x")
    else:
        return s, ()
    return _flatten(s, bar.ts, "x", why)


def _stopless(s: RunnerState, ts: datetime, suffix: str) -> Step:
    """Flatten a position with no known stop (reversed by an over-close, or not Fenrir's)."""
    why = f"position {s.position.qty} has no known stop"
    s, out = _flatten(s, ts, suffix, why)
    return s, (Log("alert", {"message": f"flattening: {why}"}), *out)


def _flatten(s: RunnerState, ts: datetime, suffix: str, why: str) -> Step:
    """The runner's own Flatten; no other goes out until flat (`exiting`)."""
    cid = client_id(s.setup.armed.account, ts, suffix, Flatten())
    return replace(s, exiting=cid), _send(s, Flatten(), cid, reason=why)


def _halt(s: RunnerState, reason: str, kind: str | None, until: date | None) -> Step:
    open_ended = s.halted is not None and s.halt_day is None
    if s.halted == reason or (open_ended and (kind != "breach" or s.halt_kind == "breach")):
        return s, ()  # an open-ended halt is replaced only by a first breach, never downgraded
    payload = {"reason": reason, "kind": kind, "until": until.isoformat() if until else None}
    out: tuple[Log, ...] = (Log("halt", payload),)
    if open_ended:  # the account was already halted: the user must still learn it failed
        out += (Log("alert", {"message": f"{reason} (was halted: {s.halted})"}),)
    return replace(s, halted=reason, halt_kind=kind, halt_day=until), out


def _roll_day(s: RunnerState, ts: datetime) -> Step:
    """Close the last trading day (one DayClosed, as the backtest) and open the day of `ts`: the
    first bar or fill of the new day, so a day closes after its own exit fills."""
    day = trading_day(ts)
    if day <= s.acct.day:
        return s, ()
    acct, unverified = _rules(s, DayClosed(s.acct.day, ts))
    s, out = _mismatched(s, unverified)
    if isinstance(acct, Breach):
        s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
        acct = s.acct  # failed until disarmed: its figures stay as they were
    elif s.nt and phase_rules(s.setup.rules, acct.phase).max_drawdown.trailing == "end_of_day":
        acct = _raised(s.setup, acct, s.nt.balance)  # NinjaTrader's balance at the close (DEC-4a)
        out += (_peak_log(s.setup, acct, ts),)  # every roll, raised or not: see _missed_close
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


def _raised(setup: Setup, a: AccountState, top: float) -> AccountState:
    """`a` with its drawdown peak raised to NinjaTrader's `top` (absolute $) and the floor
    trailing it, as drawdown.trail does; never lowered."""
    peak = top - setup.start.phase_start
    if peak <= a.peak:
        return a
    return replace(a, peak=peak, floor=max(a.floor, _floor(setup.rules, a.phase, peak)))


def _peaked(was: RunnerState, s: RunnerState, ts: datetime) -> tuple[Log, ...]:
    """A "peak" row when sim or live raised the peak (intraday): a restart starts from it."""
    if s.setup.armed.mode == "dry" or s.setup.start is None or s.acct.peak <= was.acct.peak:
        return ()
    return (_peak_log(s.setup, s.acct, ts),)


def _peak_log(setup: Setup, a: AccountState, ts: datetime) -> Log:
    """The drawdown peak in absolute $, stamped with the event that set it (UTC ISO 8601)."""
    stamp = ts.astimezone(UTC).isoformat()  # journal.iso's format
    return Log("peak", {"peak": setup.start.phase_start + a.peak, "ts": stamp})


def _strict(s: RunnerState) -> AccountState:
    """DEC-4b: `s.acct` with its equity cut to the stricter of the app's and NinjaTrader's figures
    on the same floor: the lowest of its own, NinjaTrader's (relative to phase start) and today's
    opening balance plus NinjaTrader's today's P&L. Pre-trade, safety margin and status use it."""
    a, nt = s.acct, s.nt
    if nt is None:
        return a
    equity = min(
        a.balance + a.open_pnl,
        nt.balance + nt.unrealized_pnl - s.setup.start.phase_start,
        a.day_start_balance + nt.realized_pnl_today + nt.unrealized_pnl,
    )
    return replace(a, open_pnl=equity - a.balance)


def _safety(s: RunnerState) -> Step:
    a = _strict(s)
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
    if paused := s.mismatch or s.unreconciled or s.stale or s.drift:
        return f"paused: {paused}"
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
    return check(_strict(s), rules, a, s.setup.spec, entry_price=bar.close, held_minis=0.0)
