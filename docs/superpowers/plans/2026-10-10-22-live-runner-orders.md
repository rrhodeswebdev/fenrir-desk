# Live Runner (Orders) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let armed accounts in `sim` and `live` mode send their accepted actions to NinjaTrader, feed fills back into the account state, reconcile with NinjaTrader on start and every reconnect, apply the heartbeat-loss policy, pause on LSE-vs-NinjaTrader price drift, and provide the kill switch.

**Architecture:** The plan 21 core gains pure handlers: `on_fill`, `on_nt_account`, `on_disconnect`, `on_reconciled`, `resolve`, `replay`, and a `Send(action, client_id)` effect. `client_id` is deterministic (account + bar minute + action index), so a retry or a restart that re-decides the same bar is deduped by the add-on. `LiveHub` (shell) performs `Send` through `AddonClient`, consumes the add-on stream (`connected` → reconcile, `disconnected` → pause, `account` → stricter limits, `fill` → state), and rebuilds state after a restart by folding journaled fills. Reconciliation mismatches halt the account until the user resolves them; NinjaTrader is the source of truth.

**Tech Stack:** Python 3.12, httpx, websockets, FastAPI, pytest, pytest-asyncio; NinjaTrader 8 Sim for the manual tasks.

**Spec:** docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md (§3 Data gate item 5, §8 Loop, Reconciliation, Failure handling, §12 Live runner)

**Index:** docs/superpowers/plans/README.md (contracts: plans 06, 19/20, "Plans 21–24 — live API and storage")

> **PROVISIONAL (gate):** Task 6 (gate item 5) needs the LSE live stream from plan 04/21. Read `docs/data-gate.md` before executing it.

## Global Constraints

- Functional core, imperative shell: every decision is in `fenrir/live/core.py`; `service.py` only performs effects and I/O.
- Modes: `dry` logs only; `sim` sends to a NinjaTrader sim account; `live` sends to a Lucid account (spec §8).
- Accepted actions go to NinjaTrader with a unique client order ID; the add-on ignores IDs it has seen (spec §8 step 5, §9).
- Every position is protected by a broker-side stop (the add-on places the OCO stop and target).
- Limits use the **stricter** of NinjaTrader's reported figures and the app's own computation (spec §8).
- On start and every reconnect, compare with NinjaTrader's positions and working orders; any mismatch halts that account, alerts, and blocks trading until the user resolves it. NinjaTrader is the source of truth (spec §8).
- Link lost: no new entries; open positions keep their NinjaTrader-side bracket. Heartbeat lost with an open position: keep the bracket by default; `Settings.heartbeat_policy = "flatten"` flattens (spec §8).
- LSE prices drift from NinjaTrader fills beyond a threshold → pause new entries and alert.
- Kill switch: flatten all, cancel all, disarm all accounts.
- No CLI command arms, disarms or kills; the PTY never has the session token (spec §7).
- `app.state.addon` is the shared `AddonClient`; settings are on `app.state.settings`.
- Commits: Conventional Commits, scope `live`.

## Review Focus

1. **The server restarts mid-day with a position open** → position and today's P&L come back from journaled fills, and no new entry is allowed until NinjaTrader is reconciled. Test: `test_replay_rebuilds_position_and_pnl` (Task 3), `test_sim_account_waits_for_reconcile` (Task 1).
2. **Fills happen while the link is down** (missed `fill` messages) → reconcile on reconnect finds the difference and halts; resolve adopts NinjaTrader's position. Tests: `test_mismatch_halts_then_resolve_adopts_nt` (Task 3), `test_hub_mismatch_halts_and_resolve_clears` (Task 4).
3. **NinjaTrader reports a worse day than our own figures** (commissions, manual trades, slippage) → its number drives the safety margin and pre-trade checks. Test: `test_nt_pnl_is_used_when_stricter` (Task 2).
4. **An account armed as `sim` is actually a live account (or the reverse)** → halt before any order. Test: `test_mode_mismatch_halts` (Task 2).
5. **Sending fails** (NinjaTrader down, token wrong, add-on returns an error) → an alert is journaled, the runner keeps going, and no position is assumed. Test: `test_failed_send_is_journaled` (Task 4).

---

### Task 1: Send accepted actions and apply fills

**Files:**
- Modify: `src/fenrir/live/core.py`
- Test: `tests/live/test_runner_orders.py`

**Interfaces:**
- Consumes: plan 21 core (`RunnerState`, `Log`, `Step`, `start`, `on_bar`, `_accept`, `_apply`, `_safety`, `_roll_day`, `FLAT`), `Fill`, `TradeClosed` (06), `Bracket`, `Flatten`, `Cancel` (01).
- Produces:

```python
UNRECONCILED = "unreconciled"; DRIFT = "price_drift"; DRIFT_TICKS = 4
@dataclass(frozen=True, slots=True)
class Send: action: Action; client_id: str
Effect = Log | Send
RunnerState  # new fields: opened: datetime | None = None; bracket: Bracket | None = None;
             #             nt_today_pnl: float | None = None; nt_balance: float | None = None
def client_id(account: str, ts: datetime, suffix: str) -> str     # "<sha1(account)[:6]>-<yymmddHHMM>-<suffix>", <= 24 chars
def on_fill(s: RunnerState, msg: Mapping[str, Any]) -> Step      # msg = add-on "fill" message (qty signed)
```

- [ ] **Step 1: Write the failing tests**

`tests/live/test_runner_orders.py`:

```python
from dataclasses import replace
from datetime import timedelta

import pytest

from fenrir.core.actions import Bracket
from fenrir.live import core
from fenrir.live.service import load_setup
from fixtures import INSTRUMENTS, T0, bar


def fill(qty, price, ts=T0 + timedelta(minutes=1, seconds=1), commission=0.0):
    return {"type": "fill", "account": "Sim101", "client_id": "c", "order_id": "o", "instrument": "NQ 12-26",
            "qty": qty, "price": price, "commission": commission, "ts": ts.isoformat()}


@pytest.fixture
def sim_setup(armed, settings):
    return load_setup(replace(armed, mode="sim"), settings, INSTRUMENTS)


@pytest.fixture
def ready(sim_setup):
    s, _ = core.start(sim_setup, T0)
    return replace(s, paused=s.paused - {core.UNRECONCILED})


def test_client_id_is_deterministic_and_short():
    a = core.client_id("Lucid-Account-0001", T0, "0")
    assert a == core.client_id("Lucid-Account-0001", T0, "0")
    assert a != core.client_id("Lucid-Account-0002", T0, "0")
    assert a.endswith("-2610121330-0") and len(a) <= 24


def test_sim_account_waits_for_reconcile(sim_setup):
    s, _ = core.start(sim_setup, T0)
    _, effects = core.on_bar(s, bar(T0))
    assert [e.kind for e in effects] == ["bar", "rejection"]
    assert effects[1].payload["reason"] == "paused: unreconciled"


def test_sim_bracket_is_sent(ready):
    s, effects = core.on_bar(ready, bar(T0))
    log, send = effects[1], effects[2]
    assert (log.kind, log.payload["sent"]) == ("action", True)
    assert send == core.Send(Bracket("long", 1, 19990.0, 20020.0), core.client_id("Sim101", T0, "0"))
    assert log.payload["client_id"] == send.client_id
    assert s.bracket == send.action


def test_dry_mode_never_sends(armed, settings):
    s, _ = core.start(load_setup(armed, settings, INSTRUMENTS), T0)
    _, effects = core.on_bar(s, bar(T0))
    assert not any(isinstance(e, core.Send) for e in effects)


def test_entry_then_exit_fill(ready):
    s, _ = core.on_bar(ready, bar(T0))
    s, effects = core.on_fill(s, fill(1, 20000.25))
    assert [e.kind for e in effects] == ["fill"]
    assert (s.position.qty, s.position.entry, s.position.stop, s.position.target) == (1, 20000.25, 19990.0, 20020.0)
    s, _ = core.on_fill(s, fill(-1, 19990.0, ts=T0 + timedelta(minutes=5)))
    assert s.position == core.FLAT and s.opened is None
    assert s.acct.balance == pytest.approx((19990.0 - 20000.25) * 20)


def test_same_bar_round_trip_is_flagged(ready):
    s, _ = core.on_bar(ready, bar(T0))
    s, _ = core.on_fill(s, fill(1, 20000.25, ts=T0 + timedelta(minutes=1, seconds=1)))
    s, _ = core.on_fill(s, fill(-1, 20020.0, ts=T0 + timedelta(minutes=1, seconds=40)))
    assert s.acct.possible_microscalp_pnl > 0


def test_losing_fill_breaches_and_halts(ready):
    s, _ = core.on_bar(ready, bar(T0))
    s, _ = core.on_fill(s, fill(1, 20000.0))
    s, effects = core.on_fill(s, fill(-1, 19850.0, ts=T0 + timedelta(minutes=9)))  # -$3,000
    assert s.halted.startswith("breach") and s.breach is not None
    assert "halt" in [e.kind for e in effects]


def test_entry_far_from_lse_close_pauses(ready):
    s, _ = core.on_bar(ready, bar(T0, c=20000.0))
    s, effects = core.on_fill(s, fill(1, 20002.0))  # 8 ticks away
    assert core.DRIFT in s.paused
    assert [e.kind for e in effects] == ["fill", "alert"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_runner_orders.py -q`
Expected: FAIL with `AttributeError: module 'fenrir.live.core' has no attribute 'UNRECONCILED'`

- [ ] **Step 3: Implement**

In `src/fenrir/live/core.py`:

Replace the imports block's first lines with:

```python
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fenrir.core.actions import Action, Bracket, Flatten
```

and change the rules-state import to:

```python
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Fill, Mark, Rule, TradeClosed
```

Add after `STALE = "stale_data"`:

```python
UNRECONCILED = "unreconciled"
DRIFT = "price_drift"
DRIFT_TICKS = 4  # shortcut: fixed band for LSE-vs-NinjaTrader entry drift; make it a setting if sim shows otherwise
```

Add four fields at the end of `RunnerState`:

```python
    opened: datetime | None = None          # time the current position was opened
    bracket: Bracket | None = None          # last bracket sent; its stop/target describe the next entry
    nt_today_pnl: float | None = None       # NinjaTrader realized + unrealized today
    nt_balance: float | None = None
```

Replace `Effect = Log` with:

```python
@dataclass(frozen=True, slots=True)
class Send:
    action: Action
    client_id: str


Effect = Log | Send
```

In `start`, replace

```python
        last_seen=now,
    )
```

with

```python
        last_seen=now,
        paused=frozenset() if setup.armed.mode == "dry" else frozenset({UNRECONCILED}),
    )
```

Replace `_accept` with:

```python
def client_id(account: str, ts: datetime, suffix: str) -> str:
    # deterministic, so a retry or a restart that re-decides the same bar is deduped by the add-on
    tag = hashlib.sha1(account.encode()).hexdigest()[:6]
    return f"{tag}-{ts.astimezone(UTC):%y%m%d%H%M}-{suffix}"


def _accept(s: RunnerState, a: Action, bar: Bar, i: int) -> Step:
    payload = {**_action_payload(a), "mode": s.setup.armed.mode}
    if s.setup.armed.mode == "dry":
        return s, (Log("action", {**payload, "sent": False}),)
    cid = client_id(s.setup.armed.account, bar.ts, str(i))
    if isinstance(a, Bracket):
        s = replace(s, bracket=a)
    return s, (Log("action", {**payload, "sent": True, "client_id": cid}), Send(a, cid))
```

Append:

```python
def on_fill(s: RunnerState, msg: Mapping[str, Any]) -> Step:
    ts = datetime.fromisoformat(msg["ts"])
    qty, price = int(msg["qty"]), float(msg["price"])
    s, out = _roll_day(s, ts)
    out += (Log("fill", dict(msg)),)
    pos, pv = s.position, s.setup.spec.point_value
    new_qty = pos.qty + qty
    closing = pos.qty != 0 and (pos.qty > 0) != (qty > 0)
    realized = min(abs(qty), abs(pos.qty)) * (price - pos.entry) * (1 if pos.qty > 0 else -1) * pv if closing else 0.0
    if new_qty == 0:
        position, opened = FLAT, None
    elif not closing:
        entry = (abs(pos.qty) * (pos.entry or 0.0) + abs(qty) * price) / abs(new_qty)
        b = s.bracket
        position = Position(new_qty, entry, b.stop if b else pos.stop, b.target if b else pos.target)
        opened = s.opened or ts
    elif (new_qty > 0) == (pos.qty > 0):
        position, opened = replace(pos, qty=new_qty), s.opened
    else:  # flipped through zero: the remainder is a new position with no known bracket
        position, opened = Position(new_qty, price, None, None), ts
    was_opened = s.opened
    s = replace(s, position=position, opened=opened)
    s, more = _apply(s, Fill(ts, qty, price, float(msg["commission"]), realized))
    out += more
    if new_qty == 0 and was_opened is not None:
        same_bar = was_opened.replace(second=0, microsecond=0) == ts.replace(second=0, microsecond=0)
        s, more = _apply(s, TradeClosed(was_opened, ts, realized, same_bar))
        out += more
    if pos.qty == 0 and s.bars and abs(price - s.bars[-1].close) > DRIFT_TICKS * s.setup.spec.tick:
        msg_text = (f"entry fill {price} is more than {DRIFT_TICKS} ticks from LSE close {s.bars[-1].close}; "
                    "new entries paused")
        s = replace(s, paused=s.paused | {DRIFT})
        out += (Log("alert", {"message": msg_text}),)
    s, more = _safety(s)
    return s, out + more
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/live -q`
Expected: all pass (plan 21's tests are unchanged: dry accounts start unpaused).

If `test_same_bar_round_trip_is_flagged` fails, check how plan 06's microscalp rule reads `TradeClosed`: the contract says it adds same-bar trade profit to `possible_microscalp_pnl`.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/live/core.py tests/live/test_runner_orders.py
git commit -m "feat(live): send accepted actions with deterministic client ids and apply fills

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: NinjaTrader account figures (stricter limits, mode check)

**Files:**
- Modify: `src/fenrir/live/core.py`
- Test: `tests/live/test_runner_nt.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `def on_nt_account(s: RunnerState, msg: Mapping[str, Any]) -> Step` (msg = add-on `account` message: `id, name, is_sim, balance, realized_pnl_today, unrealized_pnl`). `_limits_acct` now returns the stricter account; `account_status.balance` prefers NinjaTrader's balance.

- [ ] **Step 1: Write the failing tests**

`tests/live/test_runner_nt.py`:

```python
from dataclasses import replace

import pytest

from fenrir.live import core
from fenrir.live.service import load_setup
from fixtures import INSTRUMENTS, T0, bar


def account_msg(realized=0.0, unrealized=0.0, is_sim=True, balance=50000.0):
    return {"type": "account", "id": "Sim101", "name": "Sim101", "is_sim": is_sim, "balance": balance,
            "realized_pnl_today": realized, "unrealized_pnl": unrealized}


@pytest.fixture
def ready(armed, settings):
    s, _ = core.start(load_setup(replace(armed, mode="sim"), settings, INSTRUMENTS), T0)
    return replace(s, paused=frozenset())


def test_nt_pnl_is_used_when_stricter(ready):
    s, effects = core.on_nt_account(ready, account_msg(realized=-700.0, unrealized=-150.0))
    assert s.halted.startswith("safety margin")
    assert [e.kind for e in effects] == ["halt"]


def test_own_pnl_is_used_when_stricter(ready):
    worse = replace(ready, acct=replace(ready.acct, balance=-900.0, day_start_balance=0.0))
    s, _ = core.on_nt_account(worse, account_msg(realized=0.0))
    assert s.halted.startswith("safety margin")


def test_balance_reported_from_nt(ready):
    s, _ = core.on_nt_account(ready, account_msg(balance=51234.5))
    assert core.account_status(s)["balance"] == 51234.5


def test_mode_mismatch_halts(ready):
    s, _ = core.on_nt_account(ready, account_msg(is_sim=False))
    assert "is_sim" in s.halted and s.halt_day is None
    _, effects = core.on_bar(s, bar(T0))
    assert effects[1].kind == "rejection"


def test_nt_figures_reset_on_day_roll(ready):
    s, _ = core.on_nt_account(ready, account_msg(realized=-100.0))
    s, _ = core.on_bar(s, bar(T0.replace(day=13)))
    assert s.nt_today_pnl is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_runner_nt.py -q`
Expected: FAIL with `AttributeError: module 'fenrir.live.core' has no attribute 'on_nt_account'`

- [ ] **Step 3: Implement**

In `src/fenrir/live/core.py`, replace `_limits_acct` with:

```python
def _limits_acct(s: RunnerState) -> AccountState:
    """Stricter of our figures and NinjaTrader's: the lower today's P&L wins (spec §8)."""
    a = s.acct
    own_today = a.balance - a.day_start_balance + a.open_pnl
    if s.nt_today_pnl is None or s.nt_today_pnl >= own_today:
        return a
    return replace(a, open_pnl=a.open_pnl - (own_today - s.nt_today_pnl))
```

In `account_status`, replace `"balance": s.acct.balance,` with:

```python
        "balance": s.nt_balance if s.nt_balance is not None else s.acct.balance,
```

In `_roll_day`, replace

```python
    s = replace(s, acct=replace(s.acct, day=day, day_start_balance=s.acct.balance, traded_today=False))
```

with

```python
    s = replace(s, acct=replace(s.acct, day=day, day_start_balance=s.acct.balance, traded_today=False),
                nt_today_pnl=None)  # NinjaTrader's "today" is unknown until its next account update
```

Append:

```python
def on_nt_account(s: RunnerState, msg: Mapping[str, Any]) -> Step:
    s = replace(s, nt_today_pnl=float(msg["realized_pnl_today"]) + float(msg["unrealized_pnl"]),
                nt_balance=float(msg["balance"]))
    mode = s.setup.armed.mode
    if mode != "dry" and bool(msg["is_sim"]) != (mode == "sim"):
        return halt(s, f"armed in {mode} mode but NinjaTrader reports is_sim={msg['is_sim']}")
    return _safety(s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/live -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/live/core.py tests/live/test_runner_nt.py
git commit -m "feat(live): use the stricter of ninjatrader and own p&l and check account mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Link loss, reconciliation, resolve and replay

**Files:**
- Modify: `src/fenrir/live/core.py`
- Test: `tests/live/test_runner_link.py`

**Interfaces:**
- Consumes: Tasks 1–2, `Mismatch` (plan 20).
- Produces:

```python
def on_disconnect(s: RunnerState, policy: str, now: datetime) -> Step     # policy = Settings.heartbeat_policy
def on_reconciled(s: RunnerState, mismatches: Sequence[Mismatch]) -> Step
def resolve(s: RunnerState, nt_qty: int, nt_avg: float | None) -> Step     # adopt NinjaTrader, re-reconcile
def replay(setup: Setup, fills: Sequence[Mapping[str, Any]], now: datetime) -> Step   # restart: fold journaled fills
```

- [ ] **Step 1: Write the failing tests**

`tests/live/test_runner_link.py`:

```python
from dataclasses import replace
from datetime import timedelta

import pytest

from fenrir.bridge.reconcile import Mismatch
from fenrir.core.actions import Flatten
from fenrir.live import core
from fenrir.live.service import load_setup
from fixtures import INSTRUMENTS, T0, bar

ENTRY = {"type": "fill", "account": "Sim101", "client_id": "c", "order_id": "o", "instrument": "NQ 12-26",
         "qty": 1, "price": 20000.0, "commission": 2.0, "ts": (T0 + timedelta(minutes=1)).isoformat()}


@pytest.fixture
def sim_setup(armed, settings):
    return load_setup(replace(armed, mode="sim"), settings, INSTRUMENTS)


@pytest.fixture
def long_one(sim_setup):
    s, _ = core.start(sim_setup, T0)
    s, _ = core.on_reconciled(s, ())
    s, _ = core.on_fill(s, ENTRY)
    return s


def test_disconnect_pauses_and_keeps_bracket_by_default(long_one):
    s, effects = core.on_disconnect(long_one, "keep", T0)
    assert core.UNRECONCILED in s.paused
    assert [e.kind for e in effects] == ["alert"]
    s, effects = core.on_disconnect(s, "keep", T0)
    assert effects == ()  # one alert per outage


def test_disconnect_flatten_policy_sends_flatten(long_one):
    _, effects = core.on_disconnect(long_one, "flatten", T0)
    sends = [e for e in effects if isinstance(e, core.Send)]
    assert sends == [core.Send(Flatten(), core.client_id("Sim101", T0, "hb"))]


def test_clean_reconcile_unpauses(sim_setup):
    s, _ = core.start(sim_setup, T0)
    s, effects = core.on_reconciled(s, ())
    assert s.paused == frozenset() and effects == ()


def test_mismatch_halts_then_resolve_adopts_nt(long_one):
    s, effects = core.on_reconciled(long_one, (Mismatch("position", "runner expects 1, NinjaTrader has 2"),))
    assert s.halted.startswith("reconcile") and [e.kind for e in effects] == ["alert", "halt"]
    s, effects = core.resolve(s, 2, 20001.0)
    assert s.halted is None
    assert (s.position.qty, s.position.entry) == (2, 20001.0)
    assert core.UNRECONCILED in s.paused  # the hub re-reconciles before trading resumes
    assert [e.kind for e in effects] == ["alert"]


def test_resolve_does_not_lift_a_breach(long_one):
    s = replace(long_one, halted="breach max_drawdown: x", halt_day=None)
    s, _ = core.resolve(s, 0, None)
    assert s.halted == "breach max_drawdown: x"


def test_replay_rebuilds_position_and_pnl(sim_setup):
    exit_ = {**ENTRY, "qty": -1, "price": 20010.0, "ts": (T0 + timedelta(minutes=3)).isoformat()}
    again = {**ENTRY, "ts": (T0 + timedelta(minutes=5)).isoformat()}
    s, effects = core.replay(sim_setup, [ENTRY, exit_, again], T0 + timedelta(minutes=30))
    assert effects == ()
    assert (s.position.qty, s.position.entry) == (1, 20000.0)
    assert s.acct.balance == pytest.approx(10 * 20 - 3 * 2.0)  # gross minus three commissions
    assert core.UNRECONCILED in s.paused
    _, effects = core.on_bar(s, bar(T0 + timedelta(minutes=30)))
    assert effects[-1].kind == "bar"  # strategy sees the open position, so no new bracket
```

`test_replay_rebuilds_position_and_pnl` assumes plan 06's `Fill` handling subtracts `commission` from `balance` (index: "realized P&L net of commissions"). If plan 06 counts commission differently, follow plan 06 and adjust the expected number, not the code.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_runner_link.py -q`
Expected: FAIL with `AttributeError: module 'fenrir.live.core' has no attribute 'on_reconciled'`

- [ ] **Step 3: Implement**

In `src/fenrir/live/core.py`, add the import:

```python
from fenrir.bridge.reconcile import Mismatch
```

Append:

```python
def on_disconnect(s: RunnerState, policy: str, now: datetime) -> Step:
    if s.setup.armed.mode == "dry" or UNRECONCILED in s.paused:
        return s, ()
    s = replace(s, paused=s.paused | {UNRECONCILED})
    out: tuple[Effect, ...] = (Log("alert", {"message": "NinjaTrader link lost; no new entries until reconciled"}),)
    if policy == "flatten" and s.position.qty != 0:
        # only works if REST still reaches the add-on; otherwise the broker-side bracket stays in force
        cid = client_id(s.setup.armed.account, now, "hb")
        out += (Log("action", {"type": "Flatten", "mode": s.setup.armed.mode, "sent": True, "client_id": cid,
                               "reason": "heartbeat lost"}), Send(Flatten(), cid))
    return s, out


def on_reconciled(s: RunnerState, mismatches: Sequence[Mismatch]) -> Step:
    s = replace(s, paused=s.paused - {UNRECONCILED})
    if not mismatches:
        return s, ()
    detail = "; ".join(f"{m.kind}: {m.detail}" for m in mismatches)
    s, out = halt(s, f"reconcile: {detail}")
    return s, (Log("alert", {"message": f"reconciliation mismatch: {detail}"}), *out)


def resolve(s: RunnerState, nt_qty: int, nt_avg: float | None) -> Step:
    """The user fixed NinjaTrader by hand: adopt its position (source of truth) and re-reconcile."""
    lift = bool(s.halted) and s.halted.startswith("reconcile")
    s = replace(
        s,
        position=FLAT if nt_qty == 0 else Position(nt_qty, nt_avg, None, None),
        opened=None if nt_qty == 0 else s.opened,
        halted=None if lift else s.halted,
        halt_day=None if lift else s.halt_day,
        paused=(s.paused - {DRIFT}) | {UNRECONCILED},
    )
    return s, (Log("alert", {"message": f"resolved by user; adopted NinjaTrader position {nt_qty}"}),)


def replay(setup: Setup, fills: Sequence[Mapping[str, Any]], now: datetime) -> Step:
    """After a restart, rebuild account state by folding the fills journaled since arming."""
    s, effects = start(setup, setup.armed.armed_at)
    for f in fills:
        s, _ = on_fill(s, f)  # their effects were journaled the first time
    s, _ = _roll_day(s, now)
    return replace(s, last_seen=now), effects
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/live -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/live/core.py tests/live/test_runner_link.py
git commit -m "feat(live): pause on link loss, halt on reconcile mismatch, replay fills on restart

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Hub sends orders, follows the add-on stream, kill switch

**Files:**
- Modify: `src/fenrir/live/journal.py` (`rows` gains `kind`, `since`)
- Modify: `src/fenrir/live/service.py`
- Modify: `src/fenrir/server/app.py` (`app.state.addon`, hub client)
- Test: `tests/live/test_hub_orders.py`

**Interfaces:**
- Consumes: Tasks 1–3, `AddonClient`, `FakeAddon`, `running`, `reconcile`, `ExpectedBook`, `nt_instrument` (plan 20), `list_armed`, `delete_armed` (plan 21).
- Produces:

```python
# fenrir.live.journal
def rows(conn, account=None, day=None, kind: str | None = None, since: datetime | None = None) -> tuple[JournalRow, ...]
# fenrir.live.service
class LiveHub:
    def __init__(self, settings, instruments=None, client: AddonClient | None = None) -> None
    client: AddonClient | None; nt_connected: bool; last_heartbeat: datetime | None
    async def handle_nt(self, msg: dict) -> None
    async def reconcile(self, account: str) -> None
    async def resolve(self, account: str) -> bool          # False if the account is not running
    async def kill(self) -> tuple[bool, str | None]        # (ok, error); always disarms everything
async def run_bridge(hub: LiveHub) -> None
# app.state.addon: AddonClient(settings.nt_url, settings.nt_token), shared by plans 23 and 24
```

- [ ] **Step 1: Write the failing tests**

`tests/live/test_hub_orders.py`:

```python
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
import pytest_asyncio

from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.live import arming, core, journal
from fenrir.live.service import LiveHub
from fixtures import INSTRUMENTS, T0, bar

ACCOUNT = {"id": "Sim101", "name": "Sim101", "is_sim": True, "balance": 50000.0,
           "realized_pnl_today": 0.0, "unrealized_pnl": 0.0}
NQ = "NQ 12-26"


@pytest_asyncio.fixture
async def env(settings, armed):
    fake = FakeAddon(accounts=[ACCOUNT], heartbeat_every=0.05)
    async with running(fake) as url:
        client = AddonClient(url, fake.token, timeout=1.0, heartbeat_timeout=1.0, reconnect_delay=0.05)
        hub = LiveHub(settings, INSTRUMENTS, client)
        arming.insert_armed(hub.conn, replace(armed, mode="sim"))
        hub.sync(T0)
        yield fake, hub
        await client.aclose()


async def settle():
    await asyncio.sleep(0.2)


def kinds(hub):
    return [r.kind for r in journal.rows(hub.conn, account="Sim101")]


@pytest.mark.asyncio
async def test_connect_reconciles_then_bracket_reaches_addon(env):
    fake, hub = env
    assert core.UNRECONCILED in hub.states["Sim101"].paused
    await hub.handle_nt({"type": "connected"})
    assert hub.states["Sim101"].paused == frozenset()
    hub.handle_bar(bar(T0))
    await settle()
    [(path, body)] = fake.requests
    assert path == "/bracket"
    assert body == {"client_id": core.client_id("Sim101", T0, "0"), "account": "Sim101", "instrument": NQ,
                    "side": "long", "qty": 1, "stop": 19990.0, "target": 20020.0}


@pytest.mark.asyncio
async def test_fill_message_updates_state_and_journal(env):
    _, hub = env
    await hub.handle_nt({"type": "connected"})
    hub.handle_bar(bar(T0))
    await hub.handle_nt({"type": "fill", "account": "Sim101", "client_id": "c", "order_id": "o",
                         "instrument": NQ, "qty": 1, "price": 20000.25, "commission": 0.0,
                         "ts": (T0 + timedelta(minutes=1)).isoformat()})
    assert hub.states["Sim101"].position.qty == 1
    assert kinds(hub)[-1] == "fill"


@pytest.mark.asyncio
async def test_hub_mismatch_halts_and_resolve_clears(env):
    fake, hub = env
    fake.positions = [{"account": "Sim101", "instrument": NQ, "qty": 1, "avg_price": 20000.0}]
    await hub.handle_nt({"type": "connected"})
    assert hub.states["Sim101"].halted.startswith("reconcile")
    fake.orders = [
        {"account": "Sim101", "order_id": "s1", "client_id": "x", "instrument": NQ, "role": "stop",
         "action": "sell", "qty": 1, "price": 19990.0, "state": "Working"},
        {"account": "Sim101", "order_id": "t1", "client_id": "x", "instrument": NQ, "role": "target",
         "action": "sell", "qty": 1, "price": 20020.0, "state": "Working"},
    ]
    assert await hub.resolve("Sim101") is True
    s = hub.states["Sim101"]
    assert s.halted is None and s.paused == frozenset() and s.position.qty == 1


@pytest.mark.asyncio
async def test_disconnect_pauses_and_status_shows_link(env):
    _, hub = env
    await hub.handle_nt({"type": "connected"})
    await hub.handle_nt({"type": "heartbeat", "ts": T0.isoformat()})
    assert hub.status()["nt"]["connected"] is True and hub.status()["nt"]["last_heartbeat"] is not None
    await hub.handle_nt({"type": "disconnected", "error": "x"})
    assert core.UNRECONCILED in hub.states["Sim101"].paused
    assert hub.status()["nt"]["connected"] is False


@pytest.mark.asyncio
async def test_kill_flattens_all_and_disarms(env):
    fake, hub = env
    ok, error = await hub.kill()
    assert (ok, error) == (True, None)
    assert [p for p, _ in fake.requests] == ["/flatten-all"]
    assert arming.list_armed(hub.conn) == () and hub.states == {}
    assert kinds(hub)[-1] == "halt"


@pytest.mark.asyncio
async def test_failed_send_is_journaled(settings, armed):
    hub = LiveHub(settings, INSTRUMENTS, None)  # NinjaTrader not configured
    arming.insert_armed(hub.conn, replace(armed, mode="sim"))
    hub.sync(T0)
    hub.states["Sim101"] = replace(hub.states["Sim101"], paused=frozenset())
    hub.handle_bar(bar(T0))
    await settle()
    rows = journal.rows(hub.conn, account="Sim101")
    assert rows[-1].kind == "alert" and "send" in rows[-1].payload["message"]


@pytest.mark.asyncio
async def test_restart_replays_journaled_fills(settings, armed):
    hub = LiveHub(settings, INSTRUMENTS, None)
    arming.insert_armed(hub.conn, replace(armed, mode="sim"))
    journal.write(hub.conn, T0 + timedelta(minutes=1), "Sim101", "fill",  # after armed_at, as in real use
                  {"type": "fill", "account": "Sim101", "client_id": "c", "order_id": "o", "instrument": NQ,
                   "qty": -2, "price": 20000.0, "commission": 0.0, "ts": (T0 + timedelta(minutes=1)).isoformat()})
    hub.sync(T0 + timedelta(minutes=10))
    assert hub.states["Sim101"].position.qty == -2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_hub_orders.py -q`
Expected: FAIL with `TypeError: LiveHub.__init__() takes from 2 to 3 positional arguments but 4 were given`

- [ ] **Step 3: Extend `journal.rows`**

In `src/fenrir/live/journal.py`, replace `rows` with:

```python
def rows(
    conn: sqlite3.Connection,
    account: str | None = None,
    day: date | None = None,
    kind: str | None = None,
    since: datetime | None = None,
) -> tuple[JournalRow, ...]:
    sql, args = "SELECT id, ts, account, kind, payload_json FROM journal WHERE 1=1", []
    if account is not None:
        sql += " AND account = ?"
        args.append(account)
    if day is not None:
        start, end = day_bounds(day)
        sql += " AND ts >= ? AND ts < ?"
        args += [iso(start), iso(end)]
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if since is not None:
        sql += " AND ts >= ?"
        args.append(iso(since))
    return tuple(
        JournalRow(i, datetime.fromisoformat(ts), acc, k, json.loads(p))
        for i, ts, acc, k, p in conn.execute(sql + " ORDER BY id", args)
    )
```

- [ ] **Step 4: Extend the hub**

In `src/fenrir/live/service.py`:

Add imports:

```python
import httpx

from fenrir.bridge.client import AddonClient
from fenrir.bridge.names import nt_instrument
from fenrir.bridge.reconcile import ExpectedBook, reconcile
from fenrir.core.actions import Bracket, Flatten
from fenrir.live.arming import delete_armed
from fenrir.live.core import Send
```

Replace `LiveHub.__init__` with:

```python
    def __init__(self, settings: Settings, instruments: Instruments | None = None,
                 client: AddonClient | None = None) -> None:
        self.settings = settings
        self.instruments = instruments or load_instruments()
        self.conn = journal.open_db(settings.data_dir / "fenrir.db")
        self.states: dict[str, RunnerState] = {}
        self.broken: dict[str, tuple[Armed, str]] = {}  # rows that failed to load, so we alert once
        self.lse_connected = False
        self.last_bar_ts: datetime | None = None
        self.client = client
        self.nt_connected = False
        self.last_heartbeat: datetime | None = None
        self._reconciling: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
```

In `sync`, replace

```python
            self.states[acc], effects = core.start(setup, now)
```

with

```python
            if a.mode == "dry":
                self.states[acc], effects = core.start(setup, now)
            else:
                fills = [r.payload for r in journal.rows(self.conn, account=acc, kind="fill", since=a.armed_at)]
                self.states[acc], effects = core.replay(setup, fills, now)
```

In `tick`, append at the end of the method:

```python
        if self.nt_connected:
            for acc, s in self.states.items():
                if core.UNRECONCILED in s.paused:
                    self._spawn(self.reconcile(acc))
```

Replace `perform` with:

```python
    def perform(self, account: str, effects: tuple[Effect, ...]) -> None:
        for e in effects:
            if isinstance(e, Send):
                self._spawn(self._send(account, self._instrument(account), e))
            else:
                journal.write(self.conn, _now(), account, e.kind, e.payload)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _instrument(self, account: str) -> str:
        s = self.states[account]
        code = s.bars[-1].contract if s.bars else front_month(
            self.instruments, s.setup.strategy.market, trading_day(_now()))
        return nt_instrument(s.setup.armed.contract, code)

    async def _send(self, account: str, instrument: str, e: Send) -> None:
        try:
            if self.client is None:
                raise RuntimeError("NinjaTrader is not configured (FENRIR_NT_TOKEN)")
            a = e.action
            if isinstance(a, Bracket):
                ack = await self.client.bracket(e.client_id, account, instrument, a.side, a.qty, a.stop, a.target)
            elif isinstance(a, Flatten):
                ack = await self.client.flatten(e.client_id, account)
            else:
                ack = await self.client.cancel(e.client_id, account, a.order_id)
            if ack.error:
                raise RuntimeError(ack.error)
        except Exception as err:  # the bracket never reached NinjaTrader, so no position is assumed
            self.perform(account, (Log("alert", {"message": f"send {e.client_id} failed: {err!r}"}),))

    async def handle_nt(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "connected":
            self.nt_connected = True
            for acc in list(self.states):
                await self.reconcile(acc)
        elif kind == "disconnected":
            self.nt_connected = False
            for acc, s in list(self.states.items()):
                self.states[acc], effects = core.on_disconnect(s, self.settings.heartbeat_policy, _now())
                self.perform(acc, effects)
        elif kind == "heartbeat":
            self.last_heartbeat = _now()
        elif kind == "account" and msg.get("id") in self.states:
            self.states[msg["id"]], effects = core.on_nt_account(self.states[msg["id"]], msg)
            self.perform(msg["id"], effects)
        elif kind == "fill" and msg.get("account") in self.states:
            self.states[msg["account"]], effects = core.on_fill(self.states[msg["account"]], msg)
            self.perform(msg["account"], effects)

    async def reconcile(self, account: str) -> None:
        s = self.states.get(account)
        if s is None or self.client is None or s.setup.armed.mode == "dry" or account in self._reconciling:
            return
        self._reconciling.add(account)
        try:
            expected = ExpectedBook(account, self._instrument(account), s.position.qty)
            positions = await self.client.positions(account)
            orders = await self.client.orders(account)
        except httpx.HTTPError as e:  # unreachable is not a mismatch: stay paused and retry on the next tick
            self.perform(account, (Log("alert", {"message": f"reconcile failed: {e!r}"}),))
            return
        finally:
            self._reconciling.discard(account)
        if account in self.states:
            self.states[account], effects = core.on_reconciled(self.states[account],
                                                               reconcile(expected, positions, orders))
            self.perform(account, effects)

    async def resolve(self, account: str) -> bool:
        if account not in self.states or self.client is None:
            return False
        instrument = self._instrument(account)
        mine = [p for p in await self.client.positions(account) if p.instrument == instrument]
        qty = sum(p.qty for p in mine)
        self.states[account], effects = core.resolve(self.states[account], qty, mine[0].avg_price if mine else None)
        self.perform(account, effects)
        await self.reconcile(account)
        return True

    async def kill(self) -> tuple[bool, str | None]:
        error = None
        if self.client is not None:
            try:
                ack = await self.client.flatten_all(f"kill-{_now():%y%m%d%H%M%S}")
                error = ack.error
            except httpx.HTTPError as e:
                error = repr(e)
        for acc in {a.account for a in list_armed(self.conn)} | set(self.states) | set(self.broken):
            delete_armed(self.conn, acc)  # disarm even when NinjaTrader is unreachable
            journal.write(self.conn, _now(), acc, "halt", {"reason": "kill switch", "until": None, "error": error})
        self.states.clear()
        self.broken.clear()
        return error is None, error
```

In `status`, replace `"nt": {"connected": False, "last_heartbeat": None},` with:

```python
            "nt": {"connected": self.nt_connected, "last_heartbeat": self.last_heartbeat},
```

Add after `run_bars`:

```python
async def run_bridge(hub: LiveHub) -> None:
    async for msg in hub.client.stream():
        try:
            await hub.handle_nt(msg)
        except Exception as e:  # never let one bad message stop the stream
            for acc in list(hub.states):
                hub.perform(acc, (Log("alert", {"message": f"add-on message failed: {e!r}"}),))
```

In `live_lifespan`, after the `run_bars` lines, add:

```python
    if hub.client is not None and hub.settings.nt_token:
        tasks.append(asyncio.create_task(run_bridge(hub)))
```

- [ ] **Step 5: Share the client on the app**

In `src/fenrir/server/app.py`, add `from fenrir.bridge.client import AddonClient` and replace

```python
    app.state.live = LiveHub(settings)
```

with

```python
    app.state.addon = AddonClient(settings.nt_url, settings.nt_token)
    app.state.live = LiveHub(settings, client=app.state.addon)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/live -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/fenrir/live/journal.py src/fenrir/live/service.py src/fenrir/server/app.py tests/live/test_hub_orders.py
git commit -m "feat(live): route sim and live orders through the add-on and reconcile on connect

The hub sends accepted actions, follows the add-on stream, rebuilds
state from journaled fills after a restart, and halts accounts whose
NinjaTrader book does not match until the user resolves them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Kill switch and resolve endpoints

**Files:**
- Modify: `src/fenrir/server/routes/live.py`
- Test: `tests/live/test_live_routes.py` (append)

**Interfaces:**
- Consumes: `LiveHub.kill`, `LiveHub.resolve` (Task 4).
- Produces: `POST /api/live/kill` → `{ok: bool, error: string | null}`; `POST /api/live/resolve` `{account}` → `{ok: true}` or 404 when the account is not running.

- [ ] **Step 1: Write the failing tests**

Append to `tests/live/test_live_routes.py`:

```python
def test_kill_disarms_everything(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        app.state.live.client = None  # no NinjaTrader in this test
        body = c.post("/api/live/kill", headers=H).json()
    assert body == {"ok": True, "error": None}
    assert arming.list_armed(app.state.live.conn) == ()


def test_kill_requires_token(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.post("/api/live/kill").status_code == 401
    assert len(arming.list_armed(app.state.live.conn)) == 1


def test_resolve_unknown_account_is_404(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.post("/api/live/resolve", json={"account": "Nope"}, headers=H).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_live_routes.py -q`
Expected: FAIL with `405 Method Not Allowed` / `404` on `/api/live/kill`.

- [ ] **Step 3: Implement**

Append to `src/fenrir/server/routes/live.py`:

```python
from fastapi import HTTPException
from pydantic import BaseModel


class ResolveBody(BaseModel):
    account: str


@router.post("/kill")
async def kill(request: Request) -> dict:
    ok, error = await request.app.state.live.kill()
    return {"ok": ok, "error": error}


@router.post("/resolve")
async def resolve(request: Request, body: ResolveBody) -> dict:
    if not await request.app.state.live.resolve(body.account):
        raise HTTPException(status_code=404, detail=f"{body.account} is not running")
    return {"ok": True}
```

Move the two new imports to the top of the file with the others.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/server/routes/live.py tests/live/test_live_routes.py
git commit -m "feat(live): add kill switch and reconcile resolve endpoints

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Gate item 5 and the dry → sim progression (manual, NinjaTrader Sim)

**PROVISIONAL (gate):** needs plan 19's add-on installed on Windows, plan 04's live stream, and an LSE key.

**Files:**
- Create: `docs/live-checklist.md`
- Modify: `docs/data-gate.md` (gate item 5 result)
- Modify: `docs/CLAUDE.md` (list the new file)

- [ ] **Step 1: Write the checklist file**

`docs/live-checklist.md`:

```markdown
# Live trading progression checklist

Run in order. Do not move to the next stage until every box in the current one is checked. Record date and result next to each box.

## 1. Dry run (plan 21)
- [ ] One full session dry-run on the strategy and account type you intend to trade: a `bar` row every minute, no unexplained `alert` rows.
- [ ] Every `action` row matches what a backtest of the same minutes emits (`fenrir backtest` on that day).

## 2. Sim (NinjaTrader Sim101)
- [ ] Add-on checklist (`addon/CHECKLIST.md`) passed on this NinjaTrader install.
- [ ] Arm on Sim101 in `sim` mode. Status shows NinjaTrader connected and the account not paused within 10 s.
- [ ] First bracket: `/orders` in NinjaTrader shows the entry fill, then a stop and a target (OCO) for the same quantity.
- [ ] Fill rows appear in the journal; status position matches NinjaTrader's Positions tab.
- [ ] Stop NinjaTrader's connection (Control Center → Connections → disconnect) for 30 s: alert "link lost", entries blocked, bracket still in NinjaTrader; on reconnect the account reconciles and unpauses.
- [ ] Close the position by hand in NinjaTrader while Fenrir is stopped, restart Fenrir: account halts with a reconcile mismatch; Resolve adopts NinjaTrader's (flat) position.
- [ ] Kill switch: positions flat, orders cancelled, account disarmed.
- [ ] 16:40 ET: the add-on flattens even with Fenrir stopped.
- [ ] Five full sessions on sim with no unexplained alerts; compare journal P&L with the backtest of the same days.

## 3. Live (Lucid account)
- [ ] Gate item 5 passed (see `docs/data-gate.md`).
- [ ] Rules file `verified_on` is under 30 days old.
- [ ] Arm with the smallest contract (MES/MNQ) first; confirm the confirmation dialog shows the backtest summary.
```

Add a line to `docs/CLAUDE.md` under the bullets:

```markdown
- `live-checklist.md`: the dry → sim → live progression; `data-gate.md`: LSE verification results.
```

- [ ] **Step 2: Gate item 5 — LSE prices vs NinjaTrader sim fills**

With Fenrir running on live LSE bars and an account armed in `sim` mode on Sim101, during regular hours, let it take at least 10 entries (or place 10 one-contract market orders from a dry strategy armed in sim that enters every 5 minutes).

Then run:

```bash
uv run python -c "
from pathlib import Path
from fenrir.live import journal
conn = journal.open_db(Path('data/fenrir.db'))
bars = {r.payload['ts']: r.payload for r in journal.rows(conn, account='Sim101', kind='bar')}
for r in journal.rows(conn, account='Sim101', kind='fill'):
    f = r.payload
    minute = f['ts'][:16]
    match = [b for ts, b in bars.items() if str(ts)[:16] == minute]
    if match:
        b = match[0]
        print(f['ts'], f['price'], b['low'], b['high'], 'inside' if b['low'] - 0.5 <= f['price'] <= b['high'] + 0.5 else 'OUTSIDE')
"
```

Expected: every fill prints `inside` (within 2 ticks of the LSE bar range for that minute; 0.5 points = 2 ticks on ES/NQ). Any `OUTSIDE` row fails gate item 5.

- [ ] **Step 3: Record the result**

Append to `docs/data-gate.md` a dated "Item 5" section: number of fills compared, worst distance in ticks, and PASS or FAIL. If FAIL, live trading stays blocked (spec §3) and `DRIFT_TICKS` must not be raised to hide it.

- [ ] **Step 4: Work through stage 2 of `docs/live-checklist.md`**

Check each box with the date as you go.

- [ ] **Step 5: Commit**

```bash
git add docs/live-checklist.md docs/data-gate.md docs/CLAUDE.md
git commit -m "docs(live): add dry to live progression checklist and record gate item 5

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
