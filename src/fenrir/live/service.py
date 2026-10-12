"""Live runner shell: armed rows -> runner states, bars and clock ticks -> core, effects -> journal.

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
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fenrir.backtest.run import AccountChoice, load_account_choices
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import Instruments, front_month, load_instruments, spec
from fenrir.core.sessions import trading_day
from fenrir.data.store import SERIES
from fenrir.files import read_regular
from fenrir.live import core, journal
from fenrir.live.arming import Armed, list_armed
from fenrir.live.core import Log, RunnerState, Setup
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
    } | dict.fromkeys(("balance", "today_pnl", "limits", "position", "session"))


class LiveHub:
    """Runs every armed account on live bars. Opens nothing until something is armed:
    `<private_dir>/fenrir.db` is opened once it exists, never created here."""

    def __init__(self, settings: Settings, *, secret: str = "", step_timeout: float = 10.0):
        """`secret` (the session key): no env var holding it reaches a step child."""
        self.settings = settings
        self.conn: sqlite3.Connection | None = None
        self.instruments: Instruments | None = None
        self.states: dict[str, RunnerState] = {}
        self.broken: dict[str, tuple[Armed, str]] = {}  # alerted once; retried when the row changes
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.lse_connected = False
        self.last_bar_ts: datetime | None = None
        self._secret = secret
        self._timeout = step_timeout
        self._lock = asyncio.Lock()  # a tick and a bar never interleave
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        """Start the clock ticks; call on the event loop. Bars come from `run_bars` (04.2)."""
        # shortcut: no bar stream yet; when 21.8 wires stream_bars, add run_bars to _tasks here
        self._tasks.append(asyncio.create_task(run_ticks(self)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for account in list(self.children):
            await self._kill(account)
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    async def tick(self, now: datetime) -> None:
        async with self._lock:
            await self._sync(now)
            for account, s in list(self.states.items()):
                self._apply(account, *core.on_tick(s, now, self.settings.stale_seconds))

    async def handle_bar(self, bar: Bar) -> None:
        async with self._lock:
            self.last_bar_ts = bar.ts
            await self._sync(bar.ts)
            day = trading_day(bar.ts)
            for account, s in list(self.states.items()):
                # a running account means _start loaded the instruments
                if bar.contract != front_month(self.instruments, s.setup.spec.series, day):
                    continue
                try:
                    self._apply(account, *await self._bar(account, s, bar))
                except Exception as e:  # one account's failure must not stop the others' bar
                    log.exception("live account %s failed on a bar", account)
                    await self._kill(account)
                    error = f"{type(e).__name__}: {e}"
                    self._apply(account, *core.halt(s, f"live runner error: {error}"))

    async def _bar(self, account: str, s: RunnerState, bar: Bar) -> core.Step:
        out: tuple[Log, ...] = ()
        if _hash(s.setup.armed.strategy_path) != s.setup.armed.strategy_hash:
            s, out = core.halt(s, "strategy file changed since arming; re-arm")
        s, more = core.on_bar(s, bar)
        s, last = await self._step(account, s)
        return s, out + more + last

    def status(self) -> dict[str, Any]:
        """The `GET /api/live/status` body (plan 22 fills `nt` and `drift`)."""
        last = self.last_bar_ts
        return {
            "lse": {"connected": self.lse_connected, "last_bar_ts": last and last.isoformat()},
            "nt": {"connected": False, "last_heartbeat": None},
            "drift": {"ok": True, "ticks": None},
            "accounts": [core.account_status(s) for s in self.states.values()]
            + [_cannot_start(a, error) for a, error in self.broken.values()],
        }

    def _apply(self, account: str, s: RunnerState, effects: tuple[Log | core.Send, ...]) -> None:
        self.states[account] = s
        for e in effects:
            if isinstance(e, core.Send):
                # unreachable until 22.2/22.4 supply sim/live start figures; fail loud, never drop
                raise RuntimeError(f"order sending lands in 22.4 (#86): {e.client_id} not sent")
            journal.write(self.conn, _now(), account, e.kind, e.payload)

    async def _sync(self, now: datetime) -> None:
        if self.conn is None:
            path = self.settings.private_dir / "fenrir.db"
            if not path.exists():
                return  # nothing was ever armed
            self.conn = journal.open_db(path)
        rows = {a.account: a for a in list_armed(self.conn)}
        for account in [x for x, s in self.states.items() if rows.get(x) != s.setup.armed]:
            del self.states[account]
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
        s, out = core.start(setup, now)
        if s.started:
            try:
                await self._spawn(a)
            except Exception as e:  # isolation refusal, load error, timeout
                s, out = core.halt(s, f"cannot load the strategy: {e}")
        self._apply(a.account, s, out)

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
