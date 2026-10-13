"""Live runner shell: armed rows -> runner states; bars, fills and clock ticks -> core; effects ->
journal and the NinjaTrader add-on.

Strategy code never runs here (DEC-1): each running account has one `fenrir.live.step_child`
process, as `FENRIR_PTY_USER` when set, holding the strategy's state; its replies are untrusted
(`parse_reply`). The `armed` table is the source of truth for which accounts run: it is re-read
on every tick and every bar.
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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.bridge.client import AddonClient
from fenrir.bridge.names import nt_instrument
from fenrir.bridge.reconcile import NtOrder, NtPosition
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import Instruments, front_month, load_instruments, spec
from fenrir.core.sessions import trading_day
from fenrir.data.store import SERIES
from fenrir.files import read_regular
from fenrir.live import core, journal
from fenrir.live.arming import Armed, delete_armed, list_armed
from fenrir.live.core import Log, RunnerState, Send, Setup
from fenrir.live.step_child import ctx_json
from fenrir.rules.account import with_dll
from fenrir.rules.loader import load_account
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
LOAD_TIMEOUT = 60.0  # python start-up plus load_strategy and init
TICK_EVERY = 5.0
RESTORED = frozenset({"halt", "peak"})  # the kinds core.start reads back after a restart
RESTREAM_AFTER = TICK_EVERY  # before following the add-on stream again after a failure
DISARMED = {"reason": "disarmed", "kind": None, "until": None}  # as the disarm route journals

Stream = Callable[[Settings, Sequence[str]], AsyncIterator[Bar]]


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


def _hash(path: str) -> str:
    """sha256 of the file's bytes, read without running them; "" when unreadable."""
    try:
        return hashlib.sha256(read_regular(Path(path), STRATEGY_MAX)).hexdigest()
    except (OSError, ValueError):
        return ""


def _now() -> datetime:
    return datetime.now(UTC)


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
    } | dict.fromkeys(("balance", "nt_balance", "today_pnl", "limits", "position", "session"))


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
        self.instruments: Instruments | None = None
        self.states: dict[str, RunnerState] = {}
        self.broken: dict[str, tuple[Armed, str]] = {}  # alerted once; retried when the row changes
        self.unwritten: dict[str, tuple[Log, ...]] = {}  # RESTORED rows sqlite refused; retried
        self.retry: dict[str, Send] = {}  # a Flatten whose post timed out; resent while exiting
        self.drift_ticks: dict[str, float] = {}  # last entry fill's distance from its LSE close
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.lse_connected = False
        self.last_bar_ts: datetime | None = None
        self.nt_connected = False  # the add-on stream is up (22.5)
        self.last_heartbeat: str | None = None  # its last heartbeat's ts, as the add-on sent it
        self._secret = secret
        self._timeout = step_timeout
        self._lock = asyncio.Lock()  # a tick and a bar never interleave
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        """Start the clock ticks and, when configured, follow the add-on's event stream; call on
        the event loop. Bars come from `run_bars` (04.2)."""
        # shortcut: no bar stream yet; when 21.8 wires stream_bars, add run_bars to _tasks here
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
                    await self._reconcile(account, now)
                s = self.states[account]
                await self._apply(account, *core.on_tick(s, now, self.settings.stale_seconds))

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
                    await self._each("reconcile", lambda a: self._reconcile(a, _now()))
            case "disconnected":
                async with self._lock:
                    self.nt_connected = False
                    await self._each(
                        "a lost link",
                        lambda a: self._apply(a, *core.on_disconnect(self.states[a], msg["error"])),
                    )
            case "account":
                async with self._lock:
                    if (s := self.states.get(msg["id"])) is not None:
                        await self._apply(msg["id"], *core.on_account(s, msg, _now()))

    async def resolve(self, account: str) -> bool:
        """The Resolve button: `core.resolve` with NinjaTrader's fresh book. KeyError when the
        account is not running; False, nothing lifted, when the book cannot be read."""
        async with self._lock:
            if account not in self.states:
                raise KeyError(account)
            return await self._reconcile(account, _now(), core.resolve)

    async def kill(self) -> dict[str, Any]:
        """The kill switch: flatten and cancel everything on NinjaTrader's managed accounts
        (`/flatten-all`) first, as it matters most, then disarm every account (runner and child
        stopped), journalled per account as the disarm route does plus an "alert" with the
        outcome. Under the hub lock, so no bar or fill runs between the two. A disarm that fails
        (e.g. sqlite locked) halts every running account in memory instead. `ok` is false, with
        the errors, when `/flatten-all` did not land, when NinjaTrader is not configured while a
        sim or live account was armed, or when the disarm failed."""
        async with self._lock:
            live = any(s.setup.armed.mode != "dry" for s in self.states.values())
            flat = None
            if self.addon is not None:
                try:  # a fresh id per press: a retry inside the client is still deduped
                    ack = await self.addon.flatten_all(f"kill-{secrets.token_hex(8)}")
                    if not (ack.accepted or ack.duplicate):
                        flat = ack.error or "refused"
                except Exception as e:  # unreachable, timeout, HTTP status, malformed reply
                    flat = f"{type(e).__name__}: {e}"
            armed: Sequence[Armed] = ()
            disarm = None
            try:
                conn = self._db()
                armed = list_armed(conn) if conn else ()
                for a in armed:
                    delete_armed(conn, a.account)
                await self._sync(_now())
            except Exception as e:
                log.exception("kill switch: disarm failed")
                disarm = f"disarm failed: {type(e).__name__}: {e}"
                for account in list(self.states):  # still armed: no new entries until disarmed
                    s = self.states[account]
                    with contextlib.suppress(Exception):  # its halt row is queued and retried
                        await self._apply(account, *core.halt(s, f"kill switch: {disarm}"))
            if self.addon is None and (live or any(a.mode != "dry" for a in armed)):
                flat = "NinjaTrader add-on not configured (FENRIR_NT_TOKEN)"
            if disarm is None:
                done = f"flatten all failed: {flat}" if flat else "flattened and cancelled all"
                try:
                    for a in armed:
                        journal.write(conn, _now(), a.account, "halt", DISARMED)
                        alert = {"message": f"kill switch: disarmed; {done}"}
                        journal.write(conn, _now(), a.account, "alert", alert)
                except Exception:  # disarmed and flattened all the same: report that, not this
                    log.exception("kill switch not journalled")
            errors = [e for e in (flat, disarm) if e]
            return {"ok": not errors, "error": "; ".join(errors) or None}

    async def handle_bar(self, bar: Bar) -> None:
        async with self._lock:
            self.last_bar_ts = bar.ts
            await self._sync(bar.ts)
            day = trading_day(bar.ts)

            async def one(account: str) -> None:
                s = self.states[account]
                # a running account means _start loaded the instruments
                if bar.contract != front_month(self.instruments, s.setup.spec.series, day):
                    return
                try:
                    await self._apply(account, *await self._bar(account, s, bar))
                except Exception as e:  # one account's failure must not stop the others' bar
                    log.exception("live account %s failed on a bar", account)
                    await self._kill(account)
                    error = f"{type(e).__name__}: {e}"
                    # from the state _apply kept, so a breach it could not journal is not replaced
                    s = self.states[account]
                    try:
                        await self._apply(account, *core.halt(s, f"live runner error: {error}"))
                    except Exception:  # the journal itself failed: halted in memory, row retried
                        log.exception("live account %s halted; halt not journalled", account)

            await self._each("a bar", one)

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
        self, account: str, now: datetime, check: Callable[..., core.Step] = core.on_reconcile
    ) -> bool:
        """`check` (`core.on_reconcile` or `core.resolve`) with NinjaTrader's `/positions` and
        `/orders` for the account; False when they cannot be read. Dry runs read nothing."""
        s = self.states[account]
        if s.setup.armed.mode == "dry":
            return True
        if self.addon is None:
            return False
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
        spec = s.setup.spec  # a running account means _start loaded the instruments
        return nt_instrument(
            spec.symbol, front_month(self.instruments, spec.series, trading_day(ts))
        )

    async def _bar(self, account: str, s: RunnerState, bar: Bar) -> core.Step:
        out: tuple[Log, ...] = ()
        if _hash(s.setup.armed.strategy_path) != s.setup.armed.strategy_hash:
            s, out = core.halt(s, "strategy file changed since arming; re-arm")
        s, more = core.on_bar(s, bar)
        if not core.runs_step(bar):  # roll window: on_bar journalled the bar
            return s, out + more
        s, last = await self._step(account, s)
        return s, out + more + last

    def status(self) -> dict[str, Any]:
        """The `GET /api/live/status` body."""
        last = self.last_bar_ts
        return {
            "lse": {"connected": self.lse_connected, "last_bar_ts": last and last.isoformat()},
            "nt": {"connected": self.nt_connected, "last_heartbeat": self.last_heartbeat},
            "drift": {
                "ok": not any(s.drift for s in self.states.values()),
                "ticks": max(self.drift_ticks.values(), default=None),
            },
            "accounts": [core.account_status(s) for s in self.states.values()]
            + [_cannot_start(a, error) for a, error in self.broken.values()],
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
        for account in [x for x, s in self.states.items() if rows.get(x) != s.setup.armed]:
            del self.states[account]
            self.unwritten.pop(account, None)  # disarmed: its halts must not reach a new arming
            self.retry.pop(account, None)
            self.drift_ticks.pop(account, None)
            await self._kill(account)
        for account in [x for x, (a, _) in self.broken.items() if rows.get(x) != a]:
            del self.broken[account]
        for account, a in rows.items():
            if account not in self.states and account not in self.broken:
                await self._start(a, now)

    async def _start(self, a: Armed, now: datetime) -> None:
        try:
            self.instruments = load_instruments()
            choice = load_account_choices(self.settings.data_dir).get(
                a.rules_account, AccountChoice()
            )
            rules = with_dll(load_account(a.rules_account, self.settings.rules_dir), choice.dll_on)
            setup = Setup(
                a,
                _hash(a.strategy_path),
                rules,
                spec(self.instruments, a.contract),
                self.settings.safety_margin_pct,
            )
        except Exception as e:  # a bad rules id, contract or file: report once, run the rest
            error = f"{type(e).__name__}: {e}"
            self.broken[a.account] = (a, error)
            journal.write(
                self.conn, _now(), a.account, "alert", {"message": f"cannot start: {error}"}
            )
            return
        halts = journal.rows(self.conn, a.account, kind="halt", since=a.armed_at)
        peaks = journal.rows(self.conn, a.account, kind="peak", since=a.armed_at)
        s, out = core.start(setup, now, [r.payload for r in halts], [r.payload for r in peaks])
        today = journal.rows(self.conn, a.account, day=trading_day(now), since=a.armed_at)
        s = core.replay(s, [(r.kind, r.payload) for r in today])
        if s.started:
            try:
                await self._spawn(a)
            except Exception as e:  # isolation refusal, load error, timeout
                s, out = core.halt(s, f"cannot load the strategy: {e}")
        await self._apply(a.account, s, out)

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
        async def exchange() -> bytes:
            if request is not None:
                proc.stdin.write(json.dumps(request).encode() + b"\n")
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
    new front month. `lse_connected` is true only once a bar has arrived."""
    while True:
        day = trading_day(_now())
        try:
            instruments = load_instruments()
            contracts = [front_month(instruments, series, day) for series in SERIES]
            async for bar in stream(hub.settings, contracts):
                hub.lse_connected = True
                await hub.handle_bar(bar)
                if trading_day(_now()) != day:
                    break
        except Exception:  # keep the runner alive; the stale-data pause covers the gap
            log.exception("live bar stream failed")
        hub.lse_connected = False
        await asyncio.sleep(TICK_EVERY)
