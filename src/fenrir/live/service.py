"""Live runner shell: armed rows -> runner states; bars, fills and clock ticks -> core; effects ->
journal and the NinjaTrader add-on.

Strategy code never runs here (DEC-1): each running account has one `fenrir.live.step_child`
process, as `FENRIR_PTY_USER` when set, holding the strategy's state; its replies are untrusted
(`parse_reply`). The `armed` table is the source of truth for which accounts run: it is re-read
on every tick and every bar. The hub keeps today's bars from the feed, which replays the day
from its open on every (re)subscription, and warms each account up on them before it takes an
entry (#79).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import secrets
import signal
import sqlite3
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.bridge.client import AddonClient
from fenrir.bridge.names import nt_instrument
from fenrir.bridge.reconcile import NtOrder, NtPosition
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import (
    INSTRUMENTS_MAX,
    Instruments,
    front_month,
    in_roll_window,
    load_instruments,
    read_instruments,
    spec,
)
from fenrir.core.sessions import NY, trading_day
from fenrir.data.live import FeedStopped, stream_bars
from fenrir.data.store import SERIES, expected_days, load_bars
from fenrir.files import read_regular
from fenrir.live import arming, core, journal
from fenrir.live.arming import Armed, delete_armed, list_armed
from fenrir.live.core import Log, RunnerState, Send, Setup, Start
from fenrir.live.step_child import ctx_json, ctx_lines
from fenrir.rules.account import with_dll
from fenrir.rules.loader import RULES_MAX, read_account, rules_path
from fenrir.server import terminal
from fenrir.server.isolation import as_user
from fenrir.server.jobs_runner import strategy_user
from fenrir.settings import Settings

log = logging.getLogger(__name__)

# -P: no cwd on sys.path, so a planted fenrir/ in the repo is never imported (as job_child)
CHILD = (sys.executable, "-P", "-m", "fenrir.live.step_child")
LINE_MAX = 64 * 1024  # a longer reply fails the step
ERROR_MAX = 2048
STRATEGY_MAX = 1024 * 1024
INSTRUMENTS = Path("instruments.yaml")  # as load_instruments reads it; pinned at arming
LOAD_TIMEOUT = 60.0  # python start-up plus load_strategy and init
# a warm-up replays a day (up to ~1,380 steps, ~3 s for a trivial strategy) under the hub lock;
# meanwhile the bar feed's consumer waits (ticks buffer in the builder, which drops them as late
# past about a minute, and the socket's 30 s ping may drop the link: a full replay), and other
# accounts' bars and fills wait. A strategy that needs longer halts instead (fail closed)
WARM_TIMEOUT = 20.0
SLOW = "the replay took over {seconds:g} s; a re-arm later today replays more bars and halts again"
TICK_EVERY = 5.0
FEED_RETRY = 60.0  # seconds before resubscribing after FeedStopped, doubling up to FEED_RETRY_MAX
FEED_RETRY_MAX = 900.0
# a run is told of a new outage no sooner than this after its last feed alert: a flapping feed
# (a bar, then refused again, each minute) alerts every 15 min, not every minute
FEED_ALERT_EVERY = timedelta(minutes=15)
RESTORED = frozenset({"halt", "peak", "day_start"})  # the kinds core.start reads back
RESTREAM_AFTER = TICK_EVERY  # before following the add-on stream again after a failure
DISARMED = {"reason": "disarmed", "kind": None, "until": None}  # as the disarm route journals
# warm-up (#79): the store's last days; all of it when these hold fewer than a window of bars
HISTORY = timedelta(days=14)
WARMING = "warming up: replaying today's bars through the strategy; no new entries until done"
WAITING = (
    "warming up: today's {series} bars from LSE are not complete yet; no new entries until they are"
)
STORE_SHORT = (
    "warming up: the bar store has no {series} bars up to {day}, the last trading day before"
    " today; run `fenrir data pull`. No new entries until then"
)
STORE_ERROR = "warming up: cannot load the bars before today ({error}); no new entries until then"
WARM_ERROR = "warming up: failed ({error}); no new entries until it succeeds"
STORE_RETRY = timedelta(minutes=1)  # a short or unreadable store is loaded again no sooner

# stream_bars: (settings, contracts, *, on_auth, start, on_live): on_auth on each authentication,
# a replay of the ticks since `start` first, on_live(contract) on each live tick (#79)
Stream = Callable[..., AsyncGenerator[Bar, None]]


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")  # NaN cannot be journalled or served


def _finite(*numbers: float) -> bool:
    """Each a finite float: 1e999 parses as inf, and 10**400 overflows float()."""
    try:
        return all(math.isfinite(float(n)) for n in numbers)
    except OverflowError:
        return False


def _action(item: Any) -> Action:
    match item:
        case {
            "type": "Bracket",
            "side": str(side),
            "qty": int() | float() as qty,
            "stop": int() | float() as stop,
            "target": int() | float() as target,
            **rest,
        } if not rest and _finite(qty, stop, target):
            return Bracket(side, qty, stop, target)  # core.decide vets side and qty
        case {"type": "Flatten", **rest} if not rest:
            return Flatten()
        case {"type": "Cancel", "order_id": str(order_id), **rest} if not rest:
            return Cancel(order_id)
    raise ValueError(f"not an action: {item!r}"[:300])


def parse_reply(line: bytes) -> dict[str, Any]:
    """One step-child line -> its reply, `actions` parsed into Actions. ValueError with the
    child's error, at EOF, or for anything else: the child runs strategy code."""
    if not line:
        raise ValueError("the strategy process exited")
    try:
        reply = json.loads(line, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("JSON nested too deep") from None
    except ValueError as e:
        raise ValueError(f"not a strategy reply: {e}") from None
    match reply:
        case {"error": str(error)}:
            raise ValueError(error[:ERROR_MAX])
        case {"actions": list(items)}:
            return {"actions": tuple(map(_action, items))}
        case {"hash": str()}:
            return reply
    raise ValueError(f"not a strategy reply: {line[:200]!r}")


def _hash(path: str | Path, limit: int = STRATEGY_MAX) -> str:
    """sha256 of the file's bytes, read without running them; "" when unreadable."""
    try:
        return hashlib.sha256(read_regular(Path(path), limit)).hexdigest()
    except (OSError, ValueError):
        return ""


def _now() -> datetime:
    return datetime.now(UTC)


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _cannot_start(a: Armed, error: str) -> dict[str, Any]:
    """An AccountStatus for an armed row that could not even be set up."""
    return {
        "account": a.account,
        "mode": a.mode,
        "phase": a.phase,
        "strategy": Path(a.strategy_path).stem,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": True,
        "halt_kind": None,
        "halt_reason": f"cannot start: {error}",
        "conditions": [{"kind": None, "reason": f"cannot start: {error}"}],
    } | dict.fromkeys(("balance", "nt_balance", "today_pnl", "limits", "position", "session"))


def _waiting(a: Armed, killed: str | None) -> dict[str, Any]:
    """An AccountStatus for a sim or live row waiting for NinjaTrader's first account message
    (`killed`: the reason a failed kill or disarm halts it with when it starts)."""
    why = killed or (
        f"waiting for NinjaTrader: no account message with a balance above 0 for {a.account}"
        " yet; nothing is sent until one arrives"
    )
    return _cannot_start(a, "") | {
        "halt_reason": why,
        "conditions": [{"kind": None, "reason": why}],
    }


def _flattened(first: str | None, again: str | None) -> str | None:
    """The kill's two `/flatten-all` errors (before the hub lock, then under it) as one, naming
    which failed; None when both landed."""
    if first and again:
        if first == again:
            return f"both flatten-alls failed: {first}"
        return (
            f"first flatten-all (before the lock) failed: {first}; "
            f"second flatten-all (under the lock) failed: {again}"
        )
    if first:
        return f"first flatten-all (before the lock) failed: {first}; the second landed"
    return again and f"second flatten-all (under the lock) failed: {again}"


class LiveHub:
    """Runs every armed account on live bars. Opens nothing until something is armed:
    `<private_dir>/fenrir.db` is opened once it exists, never created here."""

    def __init__(
        self,
        settings: Settings,
        *,
        secret: str = "",
        step_timeout: float = 10.0,
        addon: AddonClient | None = None,
    ):
        """`secret` (the session key): no env var holding it reaches a step child. `addon`: the
        NinjaTrader add-on client the Sends go to; None when not configured (they fail)."""
        self.settings = settings
        self.addon = addon
        self.conn: sqlite3.Connection | None = None
        # per running account, the instruments it started with (and pinned): never another's
        self.instruments: dict[str, Instruments] = {}
        self.states: dict[str, RunnerState] = {}
        self.broken: dict[str, tuple[Armed, str]] = {}  # alerted once; retried when the row changes
        # sim/live rows set up but not started: Start.balance is NinjaTrader's first account message
        self.waiting: dict[str, tuple[Armed, Setup]] = {}
        self.failed: dict[str, str] = {}  # the error last logged per account: logged once
        self.unwritten: dict[str, tuple[Log, ...]] = {}  # RESTORED rows sqlite refused; retried
        self.retry: dict[str, Send] = {}  # a Flatten whose post timed out; resent while exiting
        self.drift_ticks: dict[str, float] = {}  # last entry fill's distance from its LSE close
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.lse_connected = False
        self.last_bar_ts: datetime | None = None
        # why the feed is down (FeedStopped), until a bar or an authenticated resubscribe
        self.lse_error: str | None = None
        self.lse_retry_at: datetime | None = None  # next resubscribe; None = only a restart
        self.today: dict[str, tuple[Bar, ...]] = {}  # per contract, its latest trading day's bars
        # series whose bars of today are still replaying since the feed (re)subscribed: no
        # account warms up on them yet, and their bars reach no account (#79)
        self.replaying: set[str] = set()
        self._lse_outage = 0  # counts outages, for the alerts
        # per run, (account, armed_at): the outage it was last told of, and when
        self._lse_alerted: dict[tuple[str, datetime], tuple[int, datetime]] = {}
        self.nt_connected = False  # the add-on stream is up (22.5)
        self.last_heartbeat: str | None = None  # its last heartbeat's ts, as the add-on sent it
        # (time, reason) of a kill whose disarm failed: a row armed by then starts halted
        self.killed: tuple[datetime, str] | None = None
        # per account, (time, reason) of a failed disarm whose halt row sqlite refused (#227)
        self.held: dict[str, tuple[datetime, str]] = {}
        self._secret = secret
        self._timeout = step_timeout
        self._lock = asyncio.Lock()  # a tick and a bar never interleave
        self._tasks: list[asyncio.Task] = []
        self._follow_bars = False  # start() was called: the first armed row starts the bar feed
        self._store_retry: dict[str, datetime] = {}  # per account: no store load before then
        self._feed: asyncio.Task | None = None  # run_bars, from then until stop()

    def start(self) -> None:
        """Start the clock ticks and, when configured, follow the add-on's event stream; call on
        the event loop. The LSE bar feed (`run_bars`) starts with the first armed row a sync
        finds: nothing reads it before, and LSE allows one socket per key."""
        self._follow_bars = True
        self._tasks.append(asyncio.create_task(run_ticks(self)))
        if self.addon is not None:
            self._tasks.append(asyncio.create_task(run_stream(self)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for r in await asyncio.gather(*self._tasks, return_exceptions=True):
            if isinstance(r, Exception):  # the type only: an add-on error could hold the token
                log.error("live task had failed: %s", type(r).__name__)
        self._tasks.clear()
        self._feed = None
        self._follow_bars = False  # a sync after stop() starts no feed that nothing cancels
        for account in list(self.children):
            await self._kill(account)
        if self.conn is not None:
            for account, rows in self.unwritten.items():  # one last try: they are lost after this
                try:
                    for r in rows:
                        journal.write(self.conn, _now(), account, r.kind, r.payload)
                except Exception:
                    log.exception("live account %s: queued rows not journalled", account)
            self.conn.close()
            self.conn = None

    async def tick(self, now: datetime) -> None:
        async with self._lock:
            await self._sync(now)

            async def one(account: str) -> None:
                # armed or restarted while linked, or its last read failed: reconcile now
                if self.states[account].unreconciled and self.nt_connected:
                    await self._reconcile(account)
                try:  # DEC-4d on every tick too: an edit is caught while no bars arrive
                    s, out = self._pinned(self.states[account])
                except Exception as e:  # e.g. a broken accounts.yaml: halts, as on a bar
                    await self._failed(account, "a tick", e)
                    return
                s, more = core.on_tick(s, now, self.settings.stale_seconds)
                effects = out + more
                if self._feed_alert_due(account, now):
                    effects = (*effects, Log("alert", {"message": self._feed_alert()}))
                await self._apply(account, s, effects)
                await self._warm(account, now)  # once today's bars and the store are there

            # e.g. its rows still refused: the others' retries still go out
            await self._each("a tick", one)

    async def handle_stream(self, msg: Mapping[str, Any]) -> None:
        """One `AddonClient.stream()` item: `connected` reconciles every account, `disconnected`
        pauses them, `account` feeds that account's figures and `fill` goes to `handle_fill`.
        `position` and `order` are not needed: a reconcile reads `/positions` and `/orders`."""
        match msg["type"]:
            case "heartbeat":
                self.last_heartbeat = msg["ts"]
            case "fill":
                await self.handle_fill(msg)
            case "connected":
                async with self._lock:
                    self.nt_connected = True
                    await self._each("reconcile", self._reconcile)
            case "disconnected":
                async with self._lock:
                    self.nt_connected = False
                    await self._each(
                        "a lost link",
                        lambda a: self._apply(a, *core.on_disconnect(self.states[a], msg["error"])),
                    )
            case "account":
                async with self._lock:
                    # its balance now: the account can start; none yet (<= 0) is not a figure
                    if msg["id"] in self.waiting and msg["balance"] > 0:
                        await self._sync(_now())  # never an arming changed since the last sync
                    if msg["id"] in self.waiting and msg["balance"] > 0:
                        a, setup = self.waiting.pop(msg["id"])
                        figures = msg["balance"], msg["realized_pnl_today"]
                        start = Start(a.start_balance, a.peak_balance, *figures)
                        await self._run(a, replace(setup, start=start), _now(), msg)
                    elif (s := self.states.get(msg["id"])) is not None:
                        await self._apply(msg["id"], *core.on_account(s, msg, _now()))

    async def resolve(self, account: str) -> bool:
        """The Resolve button: `core.resolve` with NinjaTrader's fresh book. KeyError when the
        account is not running; False, nothing lifted, when the book cannot be read."""
        async with self._lock:
            if account not in self.states:
                raise KeyError(account)
            return await self._reconcile(account, core.resolve)

    async def kill(self) -> dict[str, Any]:
        """The kill switch: flatten and cancel everything on NinjaTrader's managed accounts
        (`/flatten-all`) at once, before the hub lock, so a tick or bar holding it (a strategy
        load, a step, a post) never delays it. Then, under the lock: `/flatten-all` again, which
        closes whatever a bar opened while the kill waited, and disarm every account (runner and
        child stopped), each journalled as the disarm route does (its "halt" row before the
        delete), plus an "alert" with the outcome; no bar or fill runs between that flatten and
        the disarm. When `armed` cannot be read or a delete fails (e.g. sqlite locked), every
        running account is halted in memory instead and `killed` halts a row armed by then when
        it starts (the next tick, or for a sim or live row waiting for NinjaTrader, its first
        account message; the kill does not sync, so loads no strategy); when only
        stopping the runners fails, the running accounts are halted. A halt row that cannot be
        written does not stop the deletes. `ok` is false, with the errors, when either
        `/flatten-all` did not land, when NinjaTrader is not configured while a sim or live
        account was armed, or when any of those failed."""
        first = await self._flatten_all()
        async with self._lock:
            live = any(s.setup.armed.mode != "dry" for s in self.states.values())
            again = await self._flatten_all()
            flat = _flattened(first, again)
            conn, armed, deleted = None, (), []
            disarm = unjournalled = unstopped = None
            try:
                conn = self._db()
                armed = list_armed(conn) if conn else ()
            except Exception as e:
                log.exception("kill switch: cannot read the armed accounts")
                disarm = f"disarm failed: {_error(e)}"
            at = _now()  # after the read: every row it found was armed by then
            try:  # first, as the disarm route: a row whose delete fails stays halted
                for a in armed:
                    journal.write(conn, _now(), a.account, "halt", DISARMED)
            except Exception as e:  # still delete: a deleted row can neither run nor restart
                log.exception("kill switch: halt rows not journalled")
                unjournalled = f"halt rows not journalled: {_error(e)}"
            try:
                for a in armed:
                    delete_armed(conn, a.account)
                    deleted.append(a)
            except Exception as e:
                log.exception("kill switch: disarm failed")
                left = ", ".join(a.account for a in armed[len(deleted) :])
                disarm = f"disarm failed for {left}: {_error(e)}"
            if disarm:  # a row armed by the kill may still be: it starts halted (tick or stream)
                self.killed = (at, f"kill switch: {disarm}")
            else:  # not after a failed disarm: its sync would load pending rows' strategies
                try:
                    await self._sync(_now())
                except Exception as e:
                    log.exception("kill switch: runners not stopped")
                    unstopped = f"disarmed, but stopping the runners failed: {_error(e)}"
            if why := disarm or unstopped:  # no new entries until disarmed and stopped
                for account in list(self.states):
                    s = self.states[account]
                    with contextlib.suppress(Exception):  # its halt row is queued and retried
                        await self._apply(account, *core.halt(s, f"kill switch: {why}"))
            if self.addon is None and (live or any(a.mode != "dry" for a in armed)):
                flat = "NinjaTrader add-on not configured (FENRIR_NT_TOKEN)"
            if not flat:
                done = "flattened and cancelled all"
            elif self.addon is None:
                done = f"flatten all failed: {flat}"
            else:
                done = flat  # names which flatten-all failed
            try:
                for a in deleted:
                    alert = {"message": f"kill switch: disarmed; {done}"}
                    journal.write(conn, _now(), a.account, "alert", alert)
            except Exception:  # disarmed and flattened all the same: report that, not this
                log.exception("kill switch not journalled")
            errors = [e for e in (flat, disarm, unjournalled, unstopped) if e]
            return {"ok": not errors, "error": "; ".join(errors) or None}

    async def _flatten_all(self) -> str | None:
        """Post `/flatten-all` under a fresh id (each post its own request; a retry inside the
        client is still deduped); its error, None when it landed or no add-on is configured."""
        if self.addon is None:
            return None
        try:
            ack = await self.addon.flatten_all(f"kill-{secrets.token_hex(8)}")
        except Exception as e:  # unreachable, timeout, HTTP status, malformed reply
            return f"{type(e).__name__}: {e}"
        return None if ack.accepted or ack.duplicate else ack.error or "refused"

    async def disarm(self, account: str) -> dict[str, Any]:
        """The Disarm button (#223): a sim or live account's `/flatten` (its position closed, its
        working orders cancelled) at once, before the hub lock, as the kill does; then, under the
        lock, `/flatten` again (closing what a bar opened meanwhile), the `"halt"` row and the
        delete (`arming.disarm`) and a sync, so nothing opens after that flatten. The flatten
        under the lock decides: when it does not land, or the disarm cannot be written, the row
        stays and the account is halted (`_keep`). Dry runs never flatten. `{ok: false, error:
        null}` when the account is not armed."""
        with contextlib.suppress(Exception):  # unreadable: the read under the lock reports it
            if (a := self._armed(account)) is not None and a.mode != "dry":
                if first := await self._flatten(account):  # the one under the lock decides
                    log.warning("disarm %s: flatten before the lock failed: %s", account, first)
        async with self._lock:
            try:
                a = self._armed(account)
            except Exception as e:  # before the flatten under the lock: none may have gone out
                log.exception("disarm %s: cannot read the armed accounts", account)
                return await self._keep(
                    account,
                    f"cannot read the armed accounts: {_error(e)}; not flattened under the lock: "
                    "the position may still be open: check NinjaTrader",
                )
            if a is None:
                return {"ok": False, "error": None}
            try:
                if a.mode != "dry" and (error := await self._flatten(account)):
                    return await self._keep(
                        account,
                        f"flatten failed: {error}; the position may still be open: "
                        "check NinjaTrader",
                    )
                now = _now()
                if not arming.disarm(self.conn, account, now):
                    return {"ok": False, "error": None}
                self.held.pop(account, None)
            except Exception as e:
                log.exception("disarm %s failed", account)
                return await self._keep(account, _error(e))
            if a.mode != "dry":
                try:
                    alert = {"message": "disarmed; flattened, working orders cancelled"}
                    journal.write(self.conn, _now(), account, "alert", alert)
                except Exception:  # disarmed and flattened all the same
                    log.exception("disarm %s not journalled", account)
            try:
                await self._sync(now)
            except Exception as e:  # the row is gone: halted until the next sync stops it
                log.exception("disarm %s: runner not stopped", account)
                why = f"disarmed, but stopping the runner failed: {_error(e)}"
                if (s := self.states.get(account)) is not None:
                    with contextlib.suppress(Exception):  # its halt row is queued and retried
                        await self._apply(account, *core.halt(s, why))
                return {"ok": False, "error": why}
            return {"ok": True, "error": None}

    def _armed(self, account: str) -> Armed | None:
        conn = self._db()
        return next((a for a in list_armed(conn) if a.account == account), None) if conn else None

    async def _flatten(self, account: str) -> str | None:
        """Post `account`'s `/flatten` under a fresh id; its error, None when it landed."""
        if self.addon is None:
            return "NinjaTrader add-on not configured (FENRIR_NT_TOKEN)"
        try:
            ack = await self.addon.flatten(f"disarm-{secrets.token_hex(8)}", account)
        except Exception as e:  # unreachable, timeout, HTTP status, malformed reply
            return _error(e)
        return None if ack.accepted or ack.duplicate else ack.error or "refused"

    async def _keep(self, account: str, cause: str) -> dict[str, Any]:
        """A disarm that could not finish (call under the lock): the row stays armed and the
        account is halted, with an alert; journalled, so a restart keeps it halted. A row not
        running yet is also `held` in memory (as the kill's `killed`), so it shows the reason
        while it waits and starts halted without loading its strategy, even when sqlite refuses
        its halt row; the error says when the halt row was not journalled."""
        why = f"disarm failed: {cause}; still armed, halted until disarmed"
        alert = Log("alert", {"message": why})
        halt = {"reason": why, "kind": None, "until": None}  # as core.halt journals
        written = False  # the halt row
        try:
            if (s := self.states.get(account)) is not None:
                s, out = core.halt(s, why)
                try:
                    await self._apply(account, s, (*out, alert))  # halted in memory if refused
                finally:
                    written = Log("halt", halt) not in self.unwritten.get(account, ())
            else:  # waiting for NinjaTrader, broken, or armed since the last sync
                self.held[account] = (_now(), why)
                journal.write(self.conn, _now(), account, "halt", halt)
                written = True
                journal.write(self.conn, _now(), account, alert.kind, alert.payload)
        except Exception as e:
            if written:
                log.exception("disarm %s failed; its alert not journalled", account)
            else:
                log.exception("disarm %s failed; its halt not journalled", account)
                held = (
                    "halt held in memory, not journalled yet (retried each tick; a restart"
                    " before then lifts it)"
                    if s is not None
                    else "halt held in memory, not journalled (a restart before it starts lifts it)"
                )
                return {"ok": False, "error": f"{why}; {held}: {_error(e)}"}
        return {"ok": False, "error": why}

    async def handle_bar(self, bar: Bar) -> None:
        """One closed bar from the feed, kept as one of today's for warm-ups (#79). A bar at or
        before the last one kept for its contract is a repeat (a reconnect replays the day from
        the open) and is dropped; while its series replays, a bar reaches no account."""
        async with self._lock:
            kept = self.today.get(bar.contract, ())
            if kept and bar.ts <= kept[-1].ts:
                return
            self.last_bar_ts = bar.ts
            catching_up = bar.contract[:-3] in self.replaying
            if not catching_up:  # first: an account it starts warms up, then takes it live
                try:
                    await self._sync(bar.ts)
                except Exception:  # never up to run_bars, which would resubscribe (a full
                    # replay); the running accounts still take the bar, the next tick syncs again
                    log.exception("live sync failed on a bar")
            day = trading_day(bar.ts)
            same_day = kept and trading_day(kept[-1].ts) == day
            self.today[bar.contract] = (*kept, bar) if same_day else (bar,)
            if catching_up:
                return

            async def one(account: str) -> None:
                s = self.states[account]
                try:
                    # inside the try: a bar this account cannot even place halts it
                    if bar.contract != front_month(
                        self.instruments[account], s.setup.spec.series, day
                    ):
                        return
                    await self._apply(account, *await self._bar(account, s, bar))
                except Exception as e:  # one account's failure must not stop the others' bar
                    await self._failed(account, "a bar", e)

            await self._each("a bar", one)

    async def _failed(self, account: str, what: str, e: Exception) -> None:
        """Halt the account (open-ended, child killed) for an error in its bar or tick; call in
        the `except`. Logged once per error: a halted account whose front month cannot be found
        fails the same way on every bar."""
        error = _error(e)
        if self.failed.get(account) != error:
            self.failed[account] = error
            log.exception("live account %s failed on %s", account, what)
        await self._kill(account)
        # from the state _apply kept, so a breach it could not journal is not replaced
        s = self.states[account]
        try:
            # its child is gone: no warm-up left to wait for
            s = replace(s, warming=None)
            await self._apply(account, *core.halt(s, f"live runner error: {error}"))
        except Exception:  # the journal itself failed: halted in memory, row retried
            log.exception("live account %s halted; halt not journalled", account)

    async def _each(self, what: str, run: Callable[[str], Awaitable[Any]]) -> None:
        """`run(account)` for every running account at once, so one account's slow add-on post
        never holds back another's Flatten; an account's error is logged, the rest still run."""

        async def one(account: str) -> None:
            try:
                await run(account)
            except Exception:
                log.exception("live account %s failed on %s", account, what)

        await asyncio.gather(*map(one, list(self.states)))

    async def _reconcile(
        self, account: str, check: Callable[..., core.Step] = core.on_reconcile
    ) -> bool:
        """`check` (`core.on_reconcile` or `core.resolve`) with NinjaTrader's `/positions` and
        `/orders` for the account; False when they cannot be read. Dry runs read nothing. `now`
        (its `read_at`) is taken just before the reads, never before a wait for the lock."""
        s = self.states[account]
        if s.setup.armed.mode == "dry":
            return True
        if self.addon is None:
            return False
        now = _now()
        try:
            positions = [NtPosition(**p) for p in await self.addon.positions(account)]
            orders = [NtOrder(**o) for o in await self.addon.orders(account)]
        except Exception as e:  # unreachable, timeout, HTTP status, malformed reply
            log.warning("live account %s: cannot read NinjaTrader's book: %s", account, e)
            return False
        await self._apply(account, *check(s, self._nt(s, now), positions, orders, now))
        return True

    async def handle_fill(self, fill: Mapping[str, Any]) -> None:
        """One validated add-on `fill` message (22.5 routes the stream here): booked only by the
        sim or live account it names, and only on that account's armed instrument. Raises the
        journal's error, as `_apply` does."""
        async with self._lock:
            account = fill["account"]
            s = self.states.get(account)
            ts = datetime.fromisoformat(fill["ts"])
            if s is None or s.setup.armed.mode == "dry" or fill["instrument"] != self._nt(s, ts):
                return
            w = s.working
            if w and s.position.qty == 0 and fill["client_id"] == w.client_id:  # its entry fill
                self.drift_ticks[account] = abs(fill["price"] - w.close) / s.setup.spec.tick
            await self._apply(account, *core.on_fill(s, fill))

    def _nt(self, s: RunnerState, ts: datetime) -> str:
        """NinjaTrader's name for the account's front month on `ts`'s trading day."""
        spec, instruments = s.setup.spec, self.instruments[s.setup.armed.account]
        return nt_instrument(spec.symbol, front_month(instruments, spec.series, trading_day(ts)))

    async def _bar(self, account: str, s: RunnerState, bar: Bar) -> core.Step:
        s, out = self._pinned(s)
        s, more = core.on_bar(s, bar)
        if not core.runs_step(bar) or s.warming:  # roll window, warming up: on_bar journalled it
            return s, out + more
        s, last = await self._step(account, s)
        return s, out + more + last

    def _pinned(self, s: RunnerState) -> core.Step:
        """DEC-4d: halted while armed, not only at start, when a pinned input is not what the
        files hold now; a broken accounts.yaml raises (the caller halts the account)."""
        changed = core.changed_since_arming(self._inputs_now(s.setup))
        return core.halt(s, changed) if changed else (s, ())

    def feed_down(self, reason: str, retry_at: datetime | None) -> None:
        """The bar feed stopped (`FeedStopped`): shown in `status()` and, once per outage, as an
        alert on each running account at its next tick. `retry_at` None: only a restart helps."""
        if self.lse_error is None:  # a new outage; forget the runs that stopped
            self._lse_outage += 1
            running = {(a, s.setup.armed.armed_at) for a, s in self.states.items()}
            self._lse_alerted = {r: v for r, v in self._lse_alerted.items() if r in running}
        self.lse_connected = False
        self.lse_error, self.lse_retry_at = reason, retry_at

    def feed_up(self) -> None:
        """The feed is up again: a bar arrived, or a resubscribe authenticated."""
        self.lse_connected = True
        self.lse_error = self.lse_retry_at = None

    def resubscribed(self) -> None:
        """The bar feed authenticated (`on_auth`): it is up, and it replays today from the open
        first, so no account warms up until each series' first live tick (`feed_live`)."""
        self.feed_up()
        self.replaying = set(SERIES)

    def feed_live(self, contract: str) -> None:
        """A live tick of `contract` (`on_live`): its series' replay of today is over."""
        self.replaying.discard(contract[:-3])

    def _feed_alert_due(self, account: str, now: datetime) -> bool:
        """Whether this run (a re-arm is a new one) is told of the feed's outage now: once per
        outage, and no sooner than `FEED_ALERT_EVERY` after its last one (a flapping feed)."""
        if self.lse_error is None:
            return False
        run = (account, self.states[account].setup.armed.armed_at)
        last = self._lse_alerted.get(run)
        if last is not None and (last[0] == self._lse_outage or now - last[1] < FEED_ALERT_EVERY):
            return False
        self._lse_alerted[run] = (self._lse_outage, now)
        return True

    def _feed_alert(self) -> str:
        then = (
            "retrying with a backoff (1 to 15 min); see the live status"
            if self.lse_retry_at is not None
            else "no live bars until the server restarts"
        )
        return f"LSE feed down: {self.lse_error}; {then}"

    def _inputs_now(self, setup: Setup) -> Setup:
        """`setup` with the pinned inputs as the files hold them now (unreadable: ""); a broken
        accounts.yaml raises, which halts the account (`handle_bar`, `tick`)."""
        a = setup.armed
        return replace(
            setup,
            strategy_hash=_hash(a.strategy_path),
            rules_hash=_hash(rules_path(a.rules_account, self.settings.rules_dir), RULES_MAX),
            instruments_hash=_hash(INSTRUMENTS, INSTRUMENTS_MAX),
            dll_on=load_account_choices(self.settings.data_dir)
            .get(a.rules_account, AccountChoice())
            .dll_on,
        )

    def status(self) -> dict[str, Any]:
        """The `GET /api/live/status` body."""
        last, retry = self.last_bar_ts, self.lse_retry_at
        return {
            "lse": {
                "connected": self.lse_connected,
                "last_bar_ts": last and last.isoformat(),
                "error": self.lse_error,
                "retry_at": retry and retry.isoformat(),
            },
            "nt": {"connected": self.nt_connected, "last_heartbeat": self.last_heartbeat},
            "drift": {
                "ok": not any(s.drift for s in self.states.values()),
                "ticks": max(self.drift_ticks.values(), default=None),
            },
            "accounts": [core.account_status(s) for s in self.states.values()]
            + [_cannot_start(a, error) for a, error in self.broken.values()]
            + [_waiting(a, self._killed(a)) for a, _ in self.waiting.values()],
        }

    async def _apply(self, account: str, s: RunnerState, effects: Sequence[Log | Send]) -> None:
        """Journal each Log and post each Send, in order, after the rows queued earlier and a
        timed-out Flatten still out. The state is kept first, so a fill that arrives while a post
        is out finds its working entry. Once sqlite refuses a row, no later row is tried: the
        RESTORED ones are queued for the next call (a tick) and the error is raised at the end;
        Flattens still go out, Brackets (whose "action" row was not written) do not."""
        self.states[account] = s
        todo: list[Log | Send] = [*self.unwritten.pop(account, ())]
        retry = self.retry.pop(account, None)
        if retry is not None and s.exiting == retry.client_id:  # still the Flatten out
            todo.append(retry)
        todo += effects
        refused: Exception | None = None
        kept: list[Log] = []
        while todo:
            e = todo.pop(0)
            if isinstance(e, Send):
                todo[:0] = await self._post(account, e, refused is not None, again=e is retry)
                continue
            if refused is None:
                try:
                    journal.write(self.conn, _now(), account, e.kind, e.payload)
                    continue
                except Exception as x:  # keep what a restart restores; the next _apply retries
                    refused = x
            if e.kind in RESTORED:
                kept.append(e)
        if refused is not None:
            self.unwritten[account] = tuple(kept)
            raise refused

    async def _post(self, account: str, send: Send, unjournalled: bool, again: bool) -> list[Log]:
        """Post one Send; the rows it adds. Only a definitive refusal (`accepted` and `duplicate`
        both false, a post never made, or a `duplicate` that `_landed` / `_flat` finds did not
        land) goes through `core.send_failed`. After a timeout or any
        other error it may have landed: a Bracket stays working until a fill, the day roll or
        reconciliation, and a Flatten is resent under its client_id (`again`: no second alert)."""
        s, cid, a = self.states[account], send.client_id, send.action
        if self.addon is None:
            error = "NinjaTrader add-on not configured (FENRIR_NT_TOKEN)"
        elif unjournalled and isinstance(a, Bracket):
            error = "not sent: the journal refused its row"
        else:
            try:
                if isinstance(a, Bracket):
                    nt = self._nt(s, s.bars[-1].ts)
                    ack = await self.addon.bracket(
                        cid, account, nt, a.side, int(a.qty), a.stop, a.target
                    )
                    landed = ack.duplicate and await self._landed(account, cid, nt)
                else:
                    ack = await self.addon.flatten(cid, account)
                    landed = ack.duplicate and await self._flat(account)
            except Exception as e:  # timeout, transport, HTTP status, malformed reply
                log.warning("live account %s: send %s outcome unknown", account, cid)
                if isinstance(a, Flatten):
                    self.retry[account] = send
                if again:
                    return []
                then = "resending it" if isinstance(a, Flatten) else "its entry stays working"
                message = f"send {cid} outcome unknown: {type(e).__name__}: {e}; {then}"
                return [Log("alert", {"message": message})]
            if ack.accepted or landed:
                return []
            error = ack.error or "refused"
            if ack.duplicate and isinstance(a, Bracket):
                error = "duplicate, but NinjaTrader shows neither its entry order nor a position"
            elif ack.duplicate:
                error = "duplicate, but NinjaTrader still holds a position"
        s, out = core.send_failed(s, cid, error)
        self.states[account] = s
        return list(out)

    async def _landed(self, account: str, cid: str, nt: str) -> bool:
        """After a `duplicate` Bracket: the earlier attempt may have been refused, its reply lost.
        It landed when NinjaTrader works its entry or holds a position; unknown counts as landed."""
        try:
            orders = await self.addon.orders(account)
            positions = await self.addon.positions(account)
        except Exception:
            log.warning("live account %s: cannot check duplicate %s", account, cid)
            return True
        entry = any(o["client_id"] == cid and o["role"] == "entry" for o in orders)
        return entry or any(p["instrument"] == nt and p["qty"] for p in positions)

    async def _flat(self, account: str) -> bool:
        """After a `duplicate` Flatten: the earlier attempt may have been refused, its reply lost,
        and `exiting` would then hold back every later exit. It landed when NinjaTrader holds no
        position on the account; unknown counts as not landed, as a second /flatten never
        over-closes (at worst one more alert and Flatten)."""
        try:
            return not any(p["qty"] for p in await self.addon.positions(account))
        except Exception:
            log.warning("live account %s: cannot check a duplicate Flatten", account)
            return False

    def _db(self) -> sqlite3.Connection | None:
        """`<private_dir>/fenrir.db`, opened once it exists; None while nothing was ever armed."""
        path = self.settings.private_dir / "fenrir.db"
        if self.conn is None and path.exists():
            self.conn = journal.open_db(path)
        return self.conn

    async def _sync(self, now: datetime) -> None:
        if self._db() is None:
            return  # nothing was ever armed
        rows = {a.account: a for a in list_armed(self.conn)}
        if rows and self._follow_bars and self._feed is None:  # before any account warms up
            self.replaying = set(SERIES)  # until the feed's replay of today is over
            self._feed = asyncio.create_task(run_bars(self, stream_bars))
            self._tasks.append(self._feed)
        for account in [x for x, s in self.states.items() if rows.get(x) != s.setup.armed]:
            del self.states[account]
            self.unwritten.pop(account, None)  # disarmed: its halts must not reach a new arming
            self.retry.pop(account, None)
            self.drift_ticks.pop(account, None)
            self.instruments.pop(account, None)
            self.failed.pop(account, None)
            self._store_retry.pop(account, None)
            await self._kill(account)
        for account in [x for x, (a, _) in self.broken.items() if rows.get(x) != a]:
            del self.broken[account]
        for account in [x for x, (a, _) in self.waiting.items() if rows.get(x) != a]:
            del self.waiting[account]
            self.instruments.pop(account, None)
        for account, a in rows.items():
            if account not in self.states | self.broken | self.waiting:
                await self._start(a, now)

    async def _start(self, a: Armed, now: datetime) -> None:
        try:
            # hashed from the very bytes in use: core.start halts on a change since arming
            instruments, instruments_hash = read_instruments(INSTRUMENTS)
            choice = load_account_choices(self.settings.data_dir).get(
                a.rules_account, AccountChoice()
            )
            rules, rules_hash = read_account(a.rules_account, self.settings.rules_dir)
            setup = Setup(
                a,
                _hash(a.strategy_path),
                # the DLL choice armed with: core.start halts when accounts.yaml's differs
                with_dll(rules, bool(a.dll_on)),
                spec(instruments, a.contract),
                self.settings.safety_margin_pct,
                rules_hash=rules_hash,
                instruments_hash=instruments_hash,
                dll_on=choice.dll_on,
            )
            self.instruments[a.account] = instruments
        except Exception as e:  # a bad rules id, contract or file: report once, run the rest
            error = f"{type(e).__name__}: {e}"
            self.broken[a.account] = (a, error)
            journal.write(
                self.conn, _now(), a.account, "alert", {"message": f"cannot start: {error}"}
            )
            return
        if a.mode != "dry" and None not in (a.start_balance, a.peak_balance):
            # DEC-4a: Start.balance is NinjaTrader's balance now, from its next account message
            self.waiting[a.account] = (a, setup)
            return
        await self._run(a, setup, now)  # dry, or sim/live without balances: core.start halts

    async def _run(
        self, a: Armed, setup: Setup, now: datetime, message: Mapping[str, Any] | None = None
    ) -> None:
        """Start the account with what it journalled since arming (today's open among it, else
        NinjaTrader's), replay today and take NinjaTrader's first `message`."""

        def since(kind: str) -> list[Mapping[str, Any]]:
            return [
                r.payload for r in journal.rows(self.conn, a.account, kind=kind, since=a.armed_at)
            ]

        s, out = core.start(setup, now, since("halt"), since("peak"), since("day_start"))
        today = journal.rows(self.conn, a.account, day=trading_day(now), since=a.armed_at)
        s = core.replay(s, [(r.kind, r.payload) for r in today])
        if message is not None:
            s, more = core.on_account(s, message, now)
            out += more
        if killed := self._killed(a):  # a kill or disarm could not: no strategy loads
            s, more = core.halt(s, killed)
            out += more
        elif s.started:
            try:
                await self._spawn(a)
            except Exception as e:  # isolation refusal, load error, timeout
                s, more = core.halt(s, f"cannot load the strategy: {e}")
                out += more  # never instead of the rows before it (an open, a mode mismatch)
        # no strategy loaded: _warm drops it at once, so a halt shows alone
        await self._apply(a.account, replace(s, warming=WARMING), out)
        await self._warm(a.account, now)

    async def _warm(self, account: str, now: datetime) -> None:
        """`_warm_up`, its errors contained: never out of a sync (from a bar, up to `run_bars`,
        which would resubscribe: a full replay). One after the strategy stepped halts the account
        (`_failed`): stepped twice, its state would not be the day's."""
        try:
            await self._warm_up(account, now)
        except Exception as e:
            await self._failed(account, "a warm-up", e)

    async def _warm_up(self, account: str, now: datetime) -> None:
        """DEC-4e: replay today's bars (`today`) through the account's strategy child, actions
        suppressed, then step sees the backtest's window (`core.warm_up`, `core.warmed`). No
        new entries until then: while its series replays (`WAITING`), or while the bar store
        does not reach the last trading day before today (`STORE_SHORT`, alerted once; loaded
        again at most once per `STORE_RETRY`)."""
        s, proc = self.states[account], self.children.get(account)
        if s.warming is None:
            return
        if proc is None:  # halted, its child gone: nothing left to wait for
            return await self._apply(account, replace(s, warming=None), ())
        series, day = s.setup.spec.series, trading_day(now)
        if series in self.replaying:
            return await self._warming(account, WAITING.format(series=series))
        if now < self._store_retry.get(account, now):
            return
        instruments = self.instruments[account]
        try:
            history, last = await asyncio.to_thread(self._history, instruments, series, day)
            contract = front_month(instruments, series, day)
        except Exception as e:  # a broken or planted parquet file, a calendar that has ended
            self._store_retry[account] = now + STORE_RETRY
            return await self._warming(account, STORE_ERROR.format(error=_error(e)), alert=True)
        if not history or trading_day(history[-1].ts) < last:
            self._store_retry[account] = now + STORE_RETRY
            why = STORE_SHORT.format(series=series, day=last.isoformat())
            return await self._warming(account, why, alert=True)
        self._store_retry.pop(account, None)
        try:  # nothing stepped yet: kept warming, tried again on the next tick
            bars = [b for b in self.today.get(contract, ()) if trading_day(b.ts) == day]
            since = s.setup.armed.armed_at
            rows = [
                (r.ts, r.kind, r.payload)
                for r in journal.rows(self.conn, account, day, since=since)
            ]
            ctxs, window = core.warm_up(s, history, bars, rows)
        except Exception as e:  # tried again after STORE_RETRY; a traceback once per error
            self._store_retry[account] = now + STORE_RETRY
            error = _error(e)
            if s.warming == WARM_ERROR.format(error=error):
                log.warning("live account %s: warm-up failed again: %s", account, error)
            else:
                log.exception("live account %s: warm-up failed", account)
            return await self._warming(account, WARM_ERROR.format(error=error), alert=True)
        replayed = []
        try:
            async with asyncio.timeout(WARM_TIMEOUT):
                for c, line in zip(ctxs, ctx_lines(ctxs), strict=True):
                    reply = await self._ask(proc, line, self._timeout)
                    replayed.append((c.ts, reply["actions"]))
        except Exception as e:  # as a step error: the child is gone
            await self._kill(account)
            why = SLOW.format(seconds=WARM_TIMEOUT) if isinstance(e, TimeoutError) else e
            s = replace(s, warming=None)  # halted, child gone: no warm-up left to wait for
            return await self._apply(
                account, *core.halt(s, f"strategy error while warming up: {why}")
            )
        await self._apply(account, *core.warmed(s, window, replayed, rows))

    async def _warming(self, account: str, reason: str, alert: bool = False) -> None:
        """Still warming up for `reason`; a new one is journalled as an "alert" when `alert`."""
        s = self.states[account]
        if s.warming != reason:
            out = (Log("alert", {"message": reason}),) if alert else ()
            await self._apply(account, replace(s, warming=reason), out)

    def _history(
        self, instruments: Instruments, series: str, day: date
    ) -> tuple[tuple[Bar, ...], date]:
        """The store's last window of bars before trading day `day`, from its last `HISTORY`
        days (all of it when those reach the last trading day but hold fewer: a store begun
        lately; a stale one is never read whole), and the last trading day before `day` with a
        session outside the roll window, which the store must reach."""
        data, end = self.settings.data_dir, day - timedelta(days=1)
        sessions = [d for d in expected_days(day - HISTORY, end) if not in_roll_window(d)]
        last = sessions[-1] if sessions else end
        bars = load_bars(data, instruments, series, day - HISTORY, end)
        if bars and trading_day(bars[-1].ts) >= last and len(bars) < core.WINDOW:
            bars = load_bars(data, instruments, series, end=end)
        return bars[-core.WINDOW :], last

    def _killed(self, a: Armed) -> str | None:
        """The halt reason of a kill whose disarm failed, or of a failed disarm of `a`'s account
        whose halt was not journalled (`held`), when `a` was armed by then."""
        for mark in (self.killed, self.held.get(a.account)):
            if mark and a.armed_at <= mark[0]:
                return mark[1]
        return None

    async def _spawn(self, a: Armed) -> None:
        overlay, drop = as_user(strategy_user(self.settings))
        proc = await asyncio.create_subprocess_exec(
            *CHILD,
            a.strategy_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=terminal.child_env(os.environ, self._secret) | overlay,
            start_new_session=True,  # so its whole group can be killed
            limit=LINE_MAX,
            **drop,
        )
        self.children[a.account] = proc
        try:
            reply = await self._ask(proc, None, LOAD_TIMEOUT)
            if reply.get("hash") != a.strategy_hash:
                raise ValueError("strategy file changed since arming; re-arm")
        except BaseException:
            await self._kill(a.account)
            raise

    async def _step(self, account: str, s: RunnerState) -> core.Step:
        proc = self.children.get(account)
        stale = self.settings.stale_seconds
        if proc is None:  # never started, or its strategy failed: journal the bar only
            return core.decide(s, (), _now(), stale)
        try:
            reply = await self._ask(proc, {"ctx": ctx_json(core.ctx(s))}, self._timeout)
        except Exception as e:
            await self._kill(account)
            return core.step_failed(s, str(e))
        # now = after step: a bar that is late by then is never traded at its old close
        return core.decide(s, reply["actions"], _now(), stale)

    async def _ask(
        self, proc: asyncio.subprocess.Process, request: Any, timeout: float
    ) -> dict[str, Any]:
        """One request (None: none, the child's first line; bytes: an encoded line) and its
        reply; ValueError for a timeout, a dead child or a reply `parse_reply` refuses."""

        line = request
        if request is not None and not isinstance(request, bytes):
            line = json.dumps(request).encode() + b"\n"

        async def exchange() -> bytes:
            if line is not None:
                proc.stdin.write(line)
                await proc.stdin.drain()
            return await proc.stdout.readline()

        try:
            line = await asyncio.wait_for(exchange(), timeout)
        except TimeoutError:
            raise ValueError(f"the strategy took over {timeout:g} s") from None
        except OSError as e:  # the child died: a broken pipe
            raise ValueError(f"the strategy process exited: {e}") from None
        reply = parse_reply(line)
        if ("actions" if request else "hash") not in reply:
            raise ValueError(f"not a strategy reply: {line[:200]!r}")
        return reply

    async def _kill(self, account: str) -> None:
        proc = self.children.pop(account, None)
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # its own session: reaches anything it forked
        await proc.wait()


async def run_ticks(hub: LiveHub, every: float = TICK_EVERY) -> None:
    while True:
        try:
            await hub.tick(_now())
        except Exception:  # e.g. a locked database: the next tick tries again
            log.exception("live tick failed")
        await asyncio.sleep(every)


async def run_stream(hub: LiveHub) -> None:
    """Feed the add-on's event stream to the hub; never ends (the client reconnects). Should
    following it fail, the link counts as lost (every account paused) and it starts again."""
    while True:
        try:
            async for msg in hub.addon.stream():
                try:
                    await hub.handle_stream(msg)
                except Exception:  # e.g. a refused journal row: the next message still gets through
                    log.exception("live: add-on %s message failed", msg["type"])
        except (
            Exception
        ) as e:  # the type only: an unexpected error could hold the stream URL's token
            error = f"stream follower failed: {type(e).__name__}"
            log.error("live: add-on %s; following it again", error)
            try:
                await hub.handle_stream({"type": "disconnected", "error": error})
            except Exception:
                log.exception("live: the lost link was not applied")
        await asyncio.sleep(RESTREAM_AFTER)


async def run_bars(hub: LiveHub, stream: Stream) -> None:
    """Feed `stream`'s bars to the hub, resubscribing each trading day so a roll picks up the
    new front month. `lse_connected` is true only once a bar has arrived. The old stream is
    closed before the next subscribes: LSE allows one socket per key. On `FeedStopped` (bad
    key, quota, the key's socket still held) it resubscribes after `FEED_RETRY` s, doubling to
    `FEED_RETRY_MAX`, back to `FEED_RETRY` after a bar; a permanent one (no key) ends it. Either
    way `hub.feed_down` puts the reason in the status and alerts each account once."""
    delay = FEED_RETRY
    while True:
        day = trading_day(_now())
        try:
            instruments = load_instruments()
            contracts = [front_month(instruments, series, day) for series in SERIES]
            # an authenticated (re)subscribe ends an outage before the next bar (e.g. a weekend),
            # and each replays today from the open: the hub keeps today's bars for warm-ups (#79)
            subscribed = stream(
                hub.settings,
                contracts,
                on_auth=hub.resubscribed,
                start=datetime.combine(day - timedelta(days=1), time(18), NY),
                on_live=hub.feed_live,
            )
            async with contextlib.aclosing(subscribed) as bars:
                async for bar in bars:
                    hub.feed_up()
                    delay = FEED_RETRY
                    await hub.handle_bar(bar)
                    if trading_day(_now()) != day:
                        break
        except FeedStopped as e:
            if e.permanent:
                hub.feed_down(str(e), None)
                log.error("live bar stream stopped: %s; no live bars until the server restarts", e)
                return
            hub.feed_down(str(e), _now() + timedelta(seconds=delay))
            log.error("live bar stream stopped: %s; resubscribing in %.0f s", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, FEED_RETRY_MAX)
            continue
        except Exception:  # keep the runner alive; the stale-data pause covers the gap
            log.exception("live bar stream failed")
        hub.lse_connected = False
        await asyncio.sleep(TICK_EVERY)
