"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O or clock.

Per closed bar the shell calls `on_bar`, runs the strategy's `step` on `ctx(state)` in its own child
process (DEC-1) unless `runs_step` is false (a roll-window bar), then passes the returned actions to
`decide` (or the error to `step_failed`). Effects are `Log`s to journal and, for sim and live,
`Send`s for the shell to post to the add-on; the add-on's fills come back through `on_fill`, and a
failed post through `send_failed`. Sim and live take no new entries until NinjaTrader's book is
reconciled (`on_reconcile`), on start and after every lost link (`on_disconnect`); after a
restart `replay` rebuilds today's book from the journal. No account takes an entry until it is
warmed up (#79): `warm_up` gives the contexts `step` replays today with, `warmed` the result.
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

LIVE_KEYS = frozenset({"mode", "sent", "client_id", "reason"})  # an "action" row's, not step's

# shortcut: fixed band for LSE-close-vs-entry-fill drift; make it a setting if sim shows otherwise
DRIFT_TICKS = 4
CONFIRM_BALANCES = (
    "confirm balances: an end-of-day session close passed while the runner was down;"
    " re-arm with the confirmed peak balance"
)
NO_CLOSE = (
    "confirm balances: no NinjaTrader balance at the end-of-day session close;"
    " re-arm with the confirmed peak balance"
)
CONFIRM_LIQUIDATED = (
    "confirm balances: NinjaTrader's balance {balance:.2f} is at or below the liquidation level"
    " {level:.2f} from the armed balances and the journalled peaks; re-arm with the confirmed"
    " balances"
)
GAP = "confirm balances: a gap"  # every balance-gap halt (#219, #222): `_exit` flattens under it
CONFIRM_GAP = GAP + (
    " {at}: NinjaTrader's {figure} {nt:.2f} puts the drawdown floor at {floor:.2f}, at or above"
    " the app's equity {equity:.2f} (P&L the app never booked: a trade on another instrument, or"
    " a fill while the server was down); any position the app holds is flattened, and the whole"
    " account while NinjaTrader shows open P&L; re-arm with the confirmed balances"
)
UNRECONCILED = "not reconciled with NinjaTrader yet: no new entries"
NO_BALANCE = (
    "NinjaTrader reports a balance at or below 0, which is not a figure (its account data may not"
    " have loaded): ignored, and no new entries until one above 0"
)
LINK_LOST = "NinjaTrader link lost: no new entries until reconciled"


@dataclass(frozen=True, slots=True)
class Start:
    """NinjaTrader balances in absolute dollars for a sim or live account (DEC-4a)."""

    phase_start: float  # balance when the current phase began (armed.start_balance)
    peak: float  # highest end-of-day balance (end_of_day) or intraday equity (intraday) so far
    balance: float  # balance now
    # NinjaTrader's realized P&L today, from the same account message as `balance`: with no open
    # journalled for today, `start` opens the day at `balance - min(realized_today, 0)` (#211)
    realized_today: float | None = None


@dataclass(frozen=True, slots=True)
class DayStart:
    """Figures at today's session open, absolute $: `start` takes them from today's journalled
    "day_start" row (each roll journals one), else from NinjaTrader's figures at start."""

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
    # the pinned inputs as they are now (DEC-4d): a change since arming halts at start
    rules_hash: str  # sha256 of the rules file `rules` was read from
    instruments_hash: str  # sha256 of instruments.yaml
    dll_on: bool  # accounts.yaml's
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
    # client_id of the bracket whose entry fill last opened a position: a later fill of that
    # entry is under its stop and target (the add-on resizes them), any other add is not (#237)
    entry_id: str | None = None
    drift: str | None = None  # entry-fill drift pause: no new entries until resolved
    unreconciled: str | None = None  # sim/live: no new entries until NinjaTrader's book matches
    mismatch: str | None = None  # reconcile pause: no new entries until the user resolves it
    fills: tuple[datetime, ...] = ()  # this trading day's fill times
    opened: datetime | None = None  # first fill of the open position
    opened_balance: float = 0.0  # acct.balance before that fill, for TradeClosed
    held: tuple[Position, float] | None = None  # position and balance before this bar's exit
    nt: NtAccount | None = None  # for the stricter-of checks (DEC-4b); never for dry runs
    read_at: datetime | None = None  # the last reconcile's read: fills up to it are in its book
    # no new entries until warmed up (#79): today's bars replayed through step; the shell sets it
    warming: str | None = None
    # NinjaTrader's last balance was at or below 0 (#220): no new entries while it lasts (a pause)
    no_balance: str | None = None
    no_balance_alerted: bool = False  # one alert per run
    # NinjaTrader's intraday peak an intraday gap halt did not take, relative to phase start, as
    # last journalled in a "peak" row's "nt_peak": a later-day restart takes it (#225)
    nt_peak: float | None = None


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
    opens: Sequence[Mapping[str, Any]] = (),
) -> Step:
    """`halts`: the "halt" payloads journalled since `armed.armed_at`, oldest first; the ones
    still in force are restored (already journalled, so not again). `peaks`: the "peak" payloads
    journalled since then; the highest of them and `start.peak` is the peak (DEC-4a), with their
    "nt_peak"s (and their opens', #220) when today's open is not journalled, else with that
    open's peak (#225), and for
    `end_of_day` with every journalled open's (#220). `opens`: the "day_start" payloads
    journalled since then. Without `setup.day_start`, today's open is the
    last of them for today, so a restart mid-day never opens the day at the balance now; with
    none, NinjaTrader's balance less its realized loss today (`start.realized_today` when below
    0; a gain stays in the open, so it gives no extra daily room and `replay` never cuts it from
    the balance), journalled as a "day_start" row with an "alert"; without those figures, none
    (no brackets)."""
    a = setup.armed
    begin = None if a.mode == "dry" else setup.start
    opened: tuple[Log, ...] = ()
    replays = (
        False  # today's open is journalled: `replay` sets the app's balance, not NinjaTrader's
    )
    trailing = phase_rules(setup.rules, a.phase).max_drawdown.trailing
    if begin is not None:
        today = trading_day(now).isoformat()
        rows = [DayStart(o["balance"], o["peak"]) for o in opens if o["day"] == today]
        replays = setup.day_start is None and bool(rows)
        # a gap halt journals NinjaTrader's peak next to the app's (#225): a restart that opens at
        # NinjaTrader's balance, which holds the gap, takes it too, so its floor is never looser
        # than NinjaTrader's; one that replays the app's own balance keeps the app's peak (theirs
        # would breach it), and at least today's open's, which a start may have set from theirs
        noted = [*peaks, *opens]  # a roll during a zero notes it on its open (#220)
        theirs = [rows[-1].peak] if replays else [p["nt_peak"] for p in noted if "nt_peak" in p]
        # end_of_day: each roll journals its open with the peak it raised to, even a roll that
        # took no close and so journalled no "peak" row (#220); within an arming the peak never
        # falls, so the highest open since arming bounds it too, on a later day as well
        open_peaks = [o["peak"] for o in opens] if trailing == "end_of_day" else []
        peak = max([begin.peak, *(p["peak"] for p in peaks), *theirs, *open_peaks])
        begin = replace(begin, peak=peak)  # before the liquidation check, which so counts it
        if setup.day_start is None:
            if rows:
                setup = replace(setup, day_start=rows[-1])  # the latest: one per arming and day
            elif begin.realized_today is not None:  # before today's losses, never above the balance
                loss = min(begin.realized_today, 0.0)
                setup = replace(setup, day_start=DayStart(begin.balance - loss, begin.peak))
                message = (
                    f"no open journalled for {today}: today's opening balance is NinjaTrader's"
                    f" balance less its realized loss today, {setup.day_start.balance:.2f}"
                )
                opened = (Log("alert", {"message": message}),)
    day = setup.day_start if begin else None
    acct = start_account(setup.rules, a.phase, trading_day(now), begin, day)
    if replays and phase_rules(setup.rules, a.phase).max_drawdown.trailing == "intraday":
        # not NinjaTrader's balance, which may hold P&L the app never saw: the account message
        # after `replay` checks that (#222); `replay` raises the peak to the app's own balance
        # there, as a gain booked since the last journalled raise may be in no "peak" row
        peak = begin.peak - begin.phase_start
        acct = replace(acct, peak=peak, floor=_floor(setup.rules, a.phase, peak))
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
    refused = changed_since_arming(setup)
    if refused:  # its raise only under the armed rules, as `_peaked` (#241)
        s, more = halt(replace(s, started=False), refused)
        return s, (out if _armed_rules(setup) else ()) + more
    if a.mode != "dry" and setup.start is None:
        reason = "sim and live need the NinjaTrader start and peak balances (DEC-4a)"
        return halt(replace(s, started=False), reason)
    if begin and at_or_below(acct.balance, acct.floor):
        # a figure at start never raises a breach: it cannot start, with the app's figures fresh
        level = begin.phase_start + acct.floor
        fresh = replace(setup, start=None, day_start=None)
        s = replace(s, setup=fresh, acct=initial_state(setup.rules, a.phase, acct.day))
        reason = CONFIRM_LIQUIDATED.format(balance=begin.balance, level=level)
        s, out = halt(replace(s, started=False), reason)
        if not out and s.halted != reason:  # a restored halt holds: still say why (review of #238)
            message = f"cannot start: {reason} (still halted: {s.halted})"
            out = (Log("alert", {"message": message}),)
        return s, out
    if opened:  # so a later restart today opens the day at the same figures
        out += (*opened, _open_log(acct.day, setup.day_start, now))
    if begin and trailing == "end_of_day" and _missed_close(a.armed_at, peaks, acct.day):
        s, more = halt(s, CONFIRM_BALANCES)
        return s, out + more
    return s, out


def changed_since_arming(setup: Setup) -> str | None:
    """Why the account must not run on what it was armed with (DEC-4d), or None: `start`
    halts on it, and the hub, with the files as they are at each bar, halts a running one."""
    a = setup.armed
    if setup.strategy_hash != a.strategy_hash:
        return "strategy file changed since arming; re-arm"
    if None in (a.dll_on, a.rules_hash, a.instruments_hash):
        return "armed before dll_on and the rules and instruments.yaml hashes were pinned; re-arm"
    if setup.rules_hash != a.rules_hash:
        return "rules file changed since arming; re-arm"
    if setup.instruments_hash != a.instruments_hash:
        return "instruments.yaml changed since arming; re-arm"
    if setup.dll_on != a.dll_on:
        was, now = ("on" if on else "off" for on in (a.dll_on, setup.dll_on))
        return f"dll_on changed since arming (armed {was}, accounts.yaml now {now}); re-arm"
    return None


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


def runs_step(bar: Bar) -> bool:
    """Whether the shell runs `step` on `bar`: never on a roll-window day, which the backtest
    never loads, so live and backtest show `step` the same bars."""
    return not in_roll_window(trading_day(bar.ts))


def on_bar(s: RunnerState, bar: Bar) -> Step:
    """Roll the day, mark and risk-check the bar; a roll-window bar (`runs_step` False) is
    journalled here and never joins `bars`, as the shell then skips `step` and `decide`; so is a
    bar while warming up (#79), marked `warming`."""
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
        bars=(*s.bars, bar)[-WINDOW:] if runs_step(bar) else s.bars,
        equity=(*s.equity, (bar.ts, a.balance + a.open_pnl)),
        last_seen=bar.ts + MINUTE,
        stale=None,
    )
    s, more = _safety(s)
    out += more
    s, more = _exit(s, bar)
    out += more + _peaked(was, s, bar.ts)
    if runs_step(bar) and not s.warming:
        return s, out
    # a bar step never sees: journalled here; while warming up it is marked, never compared
    warming = {"warming": True} if s.warming else {}
    return s, (*out, Log("bar", {**asdict(bar), "actions": 0, **warming}))


def on_fill(s: RunnerState, fill: Mapping[str, Any]) -> Step:
    """Book one add-on `fill` message (validated by fenrir.bridge.client) on this account's
    instrument; the shell routes fills by account and instrument."""
    ts = datetime.fromisoformat(fill["ts"])
    qty, price = fill["qty"], fill["price"]
    s, out = _roll_day(s, ts)  # journals its own "peak" row; a Fill never moves the peak
    out += (Log("fill", dict(fill)),)
    if qty == 0:
        return s, (*out, Log("alert", {"message": "ignored a fill with qty 0"}))
    if s.read_at is not None and ts <= s.read_at:  # buffered while the book was read: in it
        # still today's fill: the "f<n>" ids count it, as a replay (which books it) does
        return replace(s, fills=(*s.fills, ts)), out
    pos, w, spec = s.position, s.working, s.setup.spec
    rest = pos.qty + qty
    flipped = rest != 0 and pos.qty != 0 and (rest > 0) != (pos.qty > 0)
    # an entry fill carries its bracket's client_id
    mine = pos.qty == 0 and w is not None and fill["client_id"] == w.client_id
    opening = pos.qty == 0 or (pos.qty > 0) == (qty > 0)
    if opening:  # or adding
        entry = (abs(pos.qty) * (pos.entry or 0.0) + abs(qty) * price) / abs(rest)
        if mine:
            stop, target = w.bracket.stop, w.bracket.target
        # more of that entry, or of one a reconcile kept working beside its first lots: the
        # add-on resizes its stop and target to cover it
        elif fill["client_id"] in (s.entry_id, w and w.client_id):
            stop, target = pos.stop, pos.target
        else:  # not Fenrir's entry (a manual add): no stop at NinjaTrader covers it all (#237)
            stop = target = None
        position, realized = Position(rest, entry, stop, target), 0.0
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
    ended = s.exiting if opening else None  # the Flatten this fill ends, which may not close it
    s = replace(
        s,
        position=position,
        fills=(*s.fills, ts),
        working=None if mine else w,
        entry_id=w.client_id if mine else s.entry_id,
        # an over-close that reversed the position ends that Flatten: the reversal gets its own;
        # so does a fill that opens or adds: a Flatten sent before it (while flat, the gap's
        # account-wide one, #229; or for fewer lots, #234) may not close all of it, and a
        # second `/flatten` never over-closes (review of #231)
        exiting=None if rest == 0 or flipped or opening else s.exiting,
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
    # "f<n>": today's n-th fill, so each fill's Flatten has its own id, the same on a replay
    if s.position.qty and s.position.stop is None and not s.exiting:  # no broker-side stop known
        s, more = _stopless(s, ts, f"f{len(s.fills)}")
        out += more
    elif ended:  # at once: no bar may call for one (a strategy's Flatten)
        why = f"position {s.position.qty} filled while Flatten {ended} was out"
        s, more = _flatten(s, ts, f"f{len(s.fills)}", why)
        out += (Log("alert", {"message": f"flattening: {why}"}), *more)
    return s, out


def on_account(s: RunnerState, account: Mapping[str, Any], now: datetime) -> Step:
    """One add-on `account` message for this account (validated by fenrir.bridge.client); dry
    runs ignore them. A sim/live mismatch halts until disarmed; otherwise the figures feed the
    stricter-of checks (DEC-4b), and an intraday peak follows NinjaTrader's equity (DEC-4a). A
    balance at or below 0 is not a figure yet: the last figures stand."""
    mode = s.setup.armed.mode
    if mode == "dry":
        return s, ()
    if account["is_sim"] != (mode == "sim"):
        theirs = "sim" if account["is_sim"] else "live"
        return halt(s, f"mode mismatch: armed {mode}, NinjaTrader reports a {theirs} account")
    if s.setup.start is None:  # could not start: no phase start to set its figures against
        return s, ()
    if account["balance"] <= 0:  # not a figure yet (NinjaTrader before its account data loads)
        return _no_balance(s)
    nt = NtAccount(account["balance"], account["realized_pnl_today"], account["unrealized_pnl"])
    was, s = s, replace(s, nt=nt, no_balance=None)
    if s.exiting and s.position.qty == 0 and round(nt.unrealized_pnl, 2) == 0:
        s = replace(s, exiting=None)  # NinjaTrader holds nothing: the gap's flatten is done (#229)
    if phase_rules(s.setup.rules, s.acct.phase).max_drawdown.trailing == "intraday":
        a, base = s.acct, s.setup.start.phase_start
        raised = _raised(s.setup, a, nt.balance + nt.unrealized_pnl)
        equity = a.balance + a.open_pnl
        # its floor alone breaches the app's figures (NinjaTrader's own equity is the peak it
        # sets, so always above it): P&L the app never saw is no breach (#222, as at the close:
        # #219); keep the app's own figures, its peak at least its own balance
        if at_or_below(equity, raised.floor) and not at_or_below(equity, a.floor):
            gap = CONFIRM_GAP.format(
                at="on NinjaTrader's account figures",
                figure="equity",
                nt=nt.balance + nt.unrealized_pnl,
                floor=base + raised.floor,
                equity=base + equity,
            )
            s, out = halt(replace(s, acct=_raised(s.setup, a, base + a.balance)), gap)
            s, out = _gap_peaked(was, s, raised.peak, now, out)
            held = s.position.qty or round(nt.unrealized_pnl, 2) != 0
            if s.halted != was.halted and held and not s.exiting:
                # flattened now, as `_exit` would on the next LSE bar, which a feed outage may
                # hold back; only on the message that raised it, so a refused send never storms
                s, more = _flatten(s, now, f"g{now.timestamp()}", s.halted)
                out += more
            return s, out
        s = replace(s, acct=raised)
    return s, _peaked(was, s, now)


def _no_balance(s: RunnerState) -> Step:
    """A balance at or below 0 (#220): NinjaTrader's last figures above 0, if any, stand, shown
    as a condition until a balance above 0, with one alert per run however often it comes back."""
    out = () if s.no_balance_alerted else (Log("alert", {"message": NO_BALANCE}),)
    return replace(s, no_balance=NO_BALANCE, no_balance_alerted=True), out


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
    one "alert" per outage, and NinjaTrader's figures are dropped as stale until its next account
    message, a zero balance's `figures` condition with them (#220). Positions keep their
    broker-side stop and target (the add-on's `flatten_on_disconnect_seconds` may flatten them:
    DEC-5). Dry runs ignore it."""
    if s.setup.armed.mode == "dry":
        return s, ()
    if s.unreconciled == LINK_LOST:  # still the same outage: no second alert, figures dropped
        return replace(s, nt=None, no_balance=None), ()
    return replace(s, unreconciled=LINK_LOST, nt=None, no_balance=None), (
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
    s = replace(s, working=w if entry else None, exiting=None, unreconciled=None, read_at=now)
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
    which already holds today's fills; an intraday peak is raised to at least that balance.
    No effects: every row was journalled when it happened.
    The drift pause stays as `start` restored it from the "halt" rows (a resolve may have lifted
    it), and sim and live stay `unreconciled` until `on_reconcile`."""
    r = replace(s, read_at=None)  # every journalled fill is booked, whatever was read since
    if s.setup.day_start is not None:
        r = replace(r, acct=replace(s.acct, balance=s.acct.day_start_balance, traded_today=False))
    close = math.nan  # the last "bar" row's close: a Bracket was checked at it
    for kind, p in rows:
        r, close = _replayed(r, kind, p, close)
    # NinjaTrader's balance at the restart holds every fill, also those made while the runner was
    # down and never journalled: never loosen it. Without day_start it already holds today's.
    booked, a = r.acct.balance, s.acct
    if s.setup.day_start is None:
        acct = replace(r.acct, balance=a.balance, traded_today=a.traded_today)
    else:
        traded = r.acct.traded_today or a.traded_today
        acct = replace(r.acct, balance=min(booked, a.balance), traded_today=traded)
    shift = acct.balance - booked  # the open trade's P&L counts from the balance kept
    if s.setup.start and phase_rules(s.setup.rules, acct.phase).max_drawdown.trailing == "intraday":
        # an intraday peak is never below the balance: a gain booked since the last raise (the
        # next Mark or account message raises it, so no "peak" row may hold it) is P&L the app
        # saw, and this balance is at most NinjaTrader's (review of #231)
        acct = _raised(s.setup, acct, s.setup.start.phase_start + acct.balance)
    # an exit's `held` marks its own bar only, which is gone
    return replace(
        r,
        acct=acct,
        opened_balance=r.opened_balance + shift,
        drift=s.drift,
        held=None,
        read_at=s.read_at,
    )


def _replayed(
    r: RunnerState, kind: str, p: Mapping[str, Any], close: float
) -> tuple[RunnerState, float]:
    """One journal row booked again: a sent order is in flight, a fill moves the position;
    `close` is the last "bar" row's, the close a Bracket was checked at."""
    if kind == "bar":
        return r, p["close"]
    if kind == "action" and p["sent"] and p["type"] == "Bracket":
        b = Bracket(p["side"], p["qty"], p["stop"], p["target"])
        return replace(r, working=Working(p["client_id"], b, close)), close
    if kind == "action" and p["sent"] and p["type"] == "Flatten":
        return replace(r, exiting=p["client_id"]), close
    if kind == "fill":
        return on_fill(r, p)[0], close
    return r, close


def warm_up(
    s: RunnerState,
    history: Sequence[Bar],
    bars: Sequence[Bar],
    rows: Sequence[tuple[datetime, str, Mapping[str, Any]]],
) -> tuple[tuple[Ctx, ...], tuple[Bar, ...]]:
    """DEC-4e: the `Ctx` `step` sees at each of today's `bars` (closed, oldest first) when today
    is replayed with its actions suppressed, and the window after the last of them. The window
    is `history` (the bar store's bars before today) then today's bars, the last WINDOW, with no
    roll-window bar, as the backtest. Position and account come from today's journal `rows`
    (write time, kind, payload; in journal order) as `replay` books them, from today's open, and
    every bar marked as live: before a bar, the rows journalled ahead of its own "bar" row (live
    booked them before it reached step), or, for a bar with none, the rows written before it
    closed."""
    a = s.acct
    r = replace(
        s,
        acct=replace(a, balance=a.day_start_balance, traded_today=False, position=0, open_pnl=0.0),
        position=FLAT,
        bars=tuple(history)[-WINDOW:],
        working=None,
        exiting=None,
        held=None,
        read_at=None,
        fills=(),
        opened=None,
        warming=None,
    )
    # live order: the rows journalled ahead of a bar's own "bar" row were booked before it
    # reached step (a fill in the feed's grace, or while the hub lock was held); a bar with no
    # row (before arming, while down) takes the rows written before it closed
    marks: dict[datetime, int] = {}
    for i, (_, kind, p) in enumerate(rows):
        if kind == "bar":
            marks.setdefault(_at(p), i)
    close, done, ctxs = math.nan, 0, []
    for b in bars:
        upto = marks.get(b.ts)
        if upto is None:
            upto = done
            while upto < len(rows) and rows[upto][0] < b.ts + MINUTE:
                upto += 1
        for _, kind, p in rows[done:upto]:
            r, close = _replayed(r, kind, p, close)
        done = max(done, upto)
        r, _ = on_bar(r, b)
        if runs_step(b):
            ctxs.append(ctx(r))
    return tuple(ctxs), r.bars


def warmed(
    s: RunnerState,
    window: Sequence[Bar],
    replayed: Sequence[tuple[datetime, Sequence[Action]]],
    rows: Sequence[tuple[datetime, str, Mapping[str, Any]]],
) -> Step:
    """Warm-up done: `window` (from `warm_up`) is `Ctx.bars`, and entries wait no longer. Each
    replayed bar's actions (`step`'s, in order) are compared with the ones journalled for it
    live; the first that differ halt the account, open-ended ("warm-up diverged"). A bar with
    none journalled (before arming, while down or warming up) is not compared."""
    s = replace(s, bars=tuple(window), warming=None)
    live = _decisions(rows)
    for ts, actions in replayed:
        got = [_payload(a) for a in actions]
        want = live.get(ts, got)
        if want != got:
            return halt(
                s, f"warm-up diverged at {ts.isoformat()}: journalled {want}, replayed {got}"
            )
    return s, ()


def _at(p: Mapping[str, Any]) -> datetime:
    """A "bar" row's bar open (journalled as str)."""
    return datetime.fromisoformat(str(p["ts"]))


def _decisions(
    rows: Sequence[tuple[datetime, str, Mapping[str, Any]]],
) -> dict[datetime, list[dict[str, Any]]]:
    """Step's actions per bar as journalled live: after each "bar" row stepped live (not one
    journalled while warming up), as many "action" / "rejection" rows as its `actions` count,
    each as the strategy returned it."""
    out: dict[datetime, list[dict[str, Any]]] = {}
    ts, left = None, 0
    for _, kind, p in rows:
        if kind == "bar":
            ts, left = _at(p), 0
            if not p.get("warming"):
                out[ts], left = [], p["actions"]
        elif kind in ("action", "rejection") and left:
            out[ts].append(
                dict(p["action"])
                if kind == "rejection"
                else {k: v for k, v in p.items() if k not in LIVE_KEYS}
            )
            left -= 1
    return out


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


def conditions(s: RunnerState) -> list[dict[str, Any]]:
    """Every condition holding new entries back, each reported even under another (#212): the
    halt (its kind; None for a halt no strip cell names), then a reconcile mismatch, a stale-data
    pause, a drift pause, NinjaTrader's balance at or below 0 ("figures", #220: a pause too) and
    warming up (#79; not a halt: `halted` stays false for it alone). The brief unreconciled pause
    is not one (only `halt_reason`)."""
    held = (
        (s.halt_kind, s.halted),
        ("reconcile", s.mismatch),
        ("stale", s.stale),
        ("drift", s.drift),
        ("figures", s.no_balance),
        ("warming", s.warming),
    )
    return [{"kind": kind, "reason": reason} for kind, reason in held if reason]


def account_status(s: RunnerState) -> dict[str, Any]:
    """One AccountStatus (Plans 21–24)."""
    a, rules, acct = s.setup.armed, s.setup.rules, s.acct
    p, v = phase_rules(rules, acct.phase), view(_strict(s), rules)
    pause = (
        "reconcile"
        if s.mismatch
        else "stale"
        if s.stale
        else "drift"
        if s.drift
        else "figures"
        if s.no_balance
        else None
    )
    head = {
        "account": a.account,
        "mode": a.mode,
        "phase": acct.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": bool(s.halted or s.mismatch or s.stale or s.drift or s.no_balance),
        "halt_kind": s.halt_kind if s.halted else pause,
        # the unreconciled and warming-up pauses show only here
        "halt_reason": s.halted
        or s.mismatch
        or s.stale
        or s.drift
        or s.no_balance
        or s.warming
        or s.unreconciled,
        "conditions": conditions(s),  # each one; halt_kind and halt_reason show the first
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
    limit (its `_dll_exit`), after a breach, under a gap halt (#219: the app cannot trust its
    figures, so it holds nothing; flat, while NinjaTrader shows open P&L, which `/flatten` closes
    account-wide: #229), or with no known stop; the add-on also flattens at 16:40 on its own.
    First, on a refused start (#241), which runs no strategy, whatever its figures say."""
    if s.exiting:
        return s, ()
    if s.position.qty and not s.started:  # no strategy runs it, on no figures of the account's
        why = f"position {s.position.qty} held by a refused start: {s.halted}"
        s, out = _flatten(s, bar.ts, "x", why)
        return s, (Log("alert", {"message": f"flattening: {why}"}), *out)
    gap = s.halted is not None and s.halted.startswith(GAP)
    if s.position.qty == 0:
        if gap and s.nt is not None and round(s.nt.unrealized_pnl, 2) != 0:
            return _flatten(s, bar.ts, "x", s.halted)
        return s, ()
    if late := _late(s, bar):
        why = f"flat-by {late}"
    elif s.acct.halted_today:
        why = "daily loss limit hit"
    elif s.halt_kind == "breach" and s.halted:
        why = s.halted
    elif gap:
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
    # an open-ended halt is replaced only by a first breach, never downgraded; and by a first gap
    # halt when it is no breach: under a strategy error, say, the gap's own row, alert and flatten
    # would be lost (#235); neither replaces a refused start's (`started` false), whose figures
    # are not the account's: `_exit` flattens what it holds on the refusal instead (#241)
    first_breach = kind == "breach" and s.halt_kind != "breach" and s.started
    gap, was_gap = reason.startswith(GAP), (s.halted or "").startswith(GAP)
    first_gap = gap and not was_gap and s.halt_kind != "breach" and s.started
    if s.halted == reason or (open_ended and not (first_breach or first_gap)):
        return s, ()
    payload = {"reason": reason, "kind": kind, "until": until.isoformat() if until else None}
    out: tuple[Log, ...] = (Log("halt", payload),)
    if open_ended:  # the account was already halted: the user must still learn it failed
        # a breach is a loss through the floor the runner checks, never the gap alone (a
        # same-bar spike halts on the account message, then that bar's own Mark breaches: review
        # of #231); true on the app's own figures and on NinjaTrader's after a later-day restart
        # (#225), so one clause for both (review of #238)
        own = ", a loss through the floor the runner checks, not the gap alone" if was_gap else ""
        out += (Log("alert", {"message": f"{reason}{own} (was halted: {s.halted})"}),)
    return replace(s, halted=reason, halt_kind=kind, halt_day=until), out


def _roll_day(s: RunnerState, ts: datetime) -> Step:
    """Close the last trading day (one DayClosed, as the backtest) and open the day of `ts`: the
    first bar or fill of the new day, so a day closes after its own exit fills."""
    day = trading_day(ts)
    if day <= s.acct.day:
        return s, ()
    acct, unverified = _rules(s, DayClosed(s.acct.day, ts))
    s, out = _mismatched(s, unverified)
    untaken = None  # NinjaTrader's raised peak under a gap at a close not taken (#220, #225)
    if isinstance(acct, Breach):
        s, out = _halt(s, f"breach {acct.rule}: {acct.reason}", "breach", None)
        acct = s.acct  # failed until disarmed: its figures stay as they were
    elif s.setup.armed.mode != "dry" and (
        phase_rules(s.setup.rules, acct.phase).max_drawdown.trailing == "end_of_day"
    ):
        # NinjaTrader's balance at the close (DEC-4a); during a balance at or below 0 (#220), its
        # last one above 0, so the floor is never looser than from those figures, though they
        # may be hours old: that close is not taken (below)
        if s.nt:
            raised = _raised(s.setup, acct, s.nt.balance)
            equity = raised.balance + raised.open_pnl
            base = s.setup.start.phase_start
            theirs = s.nt.balance + s.nt.unrealized_pnl - base  # NinjaTrader's own equity
            # its floor breaches the app's figures but not its own: a gap between the balances,
            # P&L the app never saw, is no breach (#219); keep the app's own figures
            if at_or_below(equity, raised.floor) and not at_or_below(theirs, raised.floor):
                gap = CONFIRM_GAP.format(
                    at="at the end-of-day session close",
                    figure="balance",
                    nt=s.nt.balance,
                    floor=base + raised.floor,
                    equity=base + equity,
                )
                s, more = halt(s, gap)
                out += more
                # and from its own balance at the close (DEC-4a), never looser than NinjaTrader's
                # by more than the gap: an open loss it holds still breaches (review of #221)
                acct = _raised(s.setup, acct, base + acct.balance)
                theirs = raised.peak  # journalled next to it: a later-day restart takes it (#225)
            else:
                acct, theirs = raised, None
        if s.nt and not s.no_balance:
            # every roll, raised or not: see _missed_close
            out += (_peak_log(s.setup, acct, ts, theirs),)
        elif s.setup.start:  # none yet, lost with the link, or 0: this close is never taken
            untaken = theirs if s.nt else None  # during a 0, a gap's: the open carries it
            s, more = halt(s, NO_CLOSE)
            out += more
            # the floor still trails the app's own closing balance, as under the gap, so an open
            # loss held over the close still breaches; no "peak" row: a restart asks again
            acct = _raised(s.setup, acct, s.setup.start.phase_start + acct.balance)
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
    if s.setup.armed.mode != "dry" and (start := s.setup.start) and s.halt_kind != "breach":
        opens = DayStart(start.phase_start + acct.day_start_balance, start.phase_start + acct.peak)
        # a restart today opens the day as this roll did
        out += (_open_log(day, opens, ts, s.setup.start.phase_start, untaken),)
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
    """A "peak" row when sim or live raised the peak (intraday): a restart starts from it. Only
    under the armed rules (`_armed_rules`, #241)."""
    if s.setup.armed.mode == "dry" or s.setup.start is None or s.acct.peak <= was.acct.peak:
        return ()
    if not _armed_rules(s.setup):
        return ()
    return (_peak_log(s.setup, s.acct, ts),)


def _armed_rules(setup: Setup) -> bool:
    """Whether `setup.rules` are the rules file armed (pinned, unchanged): always for a start that
    ran; for a refused one (#241) only when another pinned input caused it. A refused start with
    other rules journals no "peak" row: their trailing rule may not be the armed one, and under an
    end_of_day arming any row reads as a roll taken (`_missed_close`)."""
    return setup.rules_hash == setup.armed.rules_hash


def _gap_peaked(
    was: RunnerState, s: RunnerState, theirs: float, ts: datetime, out: tuple[Log | Send, ...]
) -> Step:
    """Under an intraday gap halt: `out`, then a "peak" row when the app's peak rose or
    NinjaTrader's `theirs` (relative), not taken, rose above the last one journalled, with it as
    "nt_peak" (#225); a later message at or below it journals nothing. Only under the armed
    rules, as `_peaked` (#241)."""
    if not _armed_rules(s.setup):
        return s, out
    if s.nt_peak is not None and theirs <= s.nt_peak:
        return s, out + _peaked(was, s, ts)
    s = replace(s, nt_peak=theirs)
    return s, (*out, _peak_log(s.setup, s.acct, ts, theirs))


def _peak_log(setup: Setup, a: AccountState, ts: datetime, theirs: float | None = None) -> Log:
    """The drawdown peak in absolute $, stamped with the event that set it (UTC ISO 8601); under
    a gap halt also NinjaTrader's raised peak `theirs` (relative), not taken, as "nt_peak"."""
    stamp = ts.astimezone(UTC).isoformat()  # journal.iso's format
    payload = {"peak": setup.start.phase_start + a.peak, "ts": stamp}
    if theirs is not None:
        payload["nt_peak"] = setup.start.phase_start + theirs
    return Log("peak", payload)


def _open_log(
    day: date, opens: DayStart, ts: datetime, base: float = 0.0, theirs: float | None = None
) -> Log:
    """Trading day `day`'s open as a "day_start" row, which `start` reads back on a restart; after
    a roll during a zero under a gap (#220), also NinjaTrader's raised peak `theirs` (relative to
    `base`, the phase start) as "nt_peak", which no "peak" row then holds (#225)."""
    stamp = ts.astimezone(UTC).isoformat()  # journal.iso's format
    payload = {"day": day.isoformat(), "balance": opens.balance, "peak": opens.peak, "ts": stamp}
    if theirs is not None:
        payload["nt_peak"] = base + theirs
    return Log("day_start", payload)


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
    if paused := s.mismatch or s.unreconciled or s.stale or s.drift or s.no_balance or s.warming:
        return f"paused: {paused}"
    if old:  # a late or replayed bar: never trade at its old close
        return old
    if s.bars[-1].ts + MINUTE != s.last_seen:  # e.g. a roll-window bar never joins `bars`
        return "not the latest bar: no entry at an older bar's close"
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
    return check(_strict(s), rules, a, s.setup.spec, entry_price=bar.close, held_minis=0.0)
