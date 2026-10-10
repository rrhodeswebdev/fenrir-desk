# Backtest Fold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simulate one prop-firm attempt: starting on a given bar, fold a strategy over 1-minute bars with a conservative fill model and the account's rule functions, through evaluation and funded phases, until a breach, the first eligible payout, or the end of the data.

**Architecture:** `fenrir.backtest.fills` holds the pure fill model (entry, stop/target, market exit prices, P&L). `fenrir.backtest.attempt.run_attempt` is a pure fold over bars: each bar passes through fixed stages (roll day → fill pending orders → stop/target → flat-by → mark-to-market → strategy decision), each stage returning a new immutable `_Sim` or a finished `AttemptResult`. Account rules come from plan 06 (`apply`, `passed`, `payout`, `pretrade.check`), so the backtester never re-implements a rule.

**Tech Stack:** Python 3.12, pytest, Hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§6 Simulation, Attempts; §8 16:40 flatten)

**Index:** `docs/superpowers/plans/README.md` (contracts: plan 01 core, plan 05 `AccountRules`, plan 06 rules API, plan 07 `fenrir.strategy`)

## Global Constraints

- Python 3.12, `uv`; `uv run pytest -q`; `uv run ruff check . && uv run ruff format --check .`.
- Functional core: this whole plan is pure. No I/O, clock, randomness or mutation; `@dataclass(frozen=True, slots=True)` values; `dataclasses.replace` for updates.
- Fill model (spec §6, always the conservative choice):
  - Bracket entry fills at the **next** bar's open plus 1 tick slippage (long: `open + tick`, short: `open - tick`).
  - If stop and target are both inside one bar, the **stop fills first**.
  - Stops and market exits get 1 tick slippage; targets fill at the limit price.
  - Commissions from the rules file: `costs.commission_per_side[symbol]` per contract per side.
  - A trade that opens and closes in the same bar is flagged as a possible microscalp.
- Positions are flat by `min(rules.session.flat_by, "16:40")` ET: the NinjaTrader add-on flattens at 16:40 on its own (spec §8–9), so backtests flatten no later than live does.
- Every `Bracket` passes `fenrir.rules.pretrade.check` before it becomes an order, exactly as live (plan 21) will.
- One position at a time; `Bracket` while in a position or with an order pending is ignored.
- Attempt phases (spec §6 Attempts): evaluation until pass or breach; funded until first eligible payout or breach. Accounts with `evaluation: null` (Lucid Direct) start funded.
- Commits: Conventional Commits, scope `backtest`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Price gaps through the stop (bar opens beyond it) → exit at `open ∓ tick`, never at the better stop price. Test in Task 2 (`test_gap_through_stop_fills_at_open`).
2. A missing stretch of data crosses a trading-day boundary while a position is open (flat-by bar never arrived) → the position is closed at the first available bar's open before the day is closed, so it never carries into a new day or phase. Test in Task 3 (`test_data_gap_closes_position_before_day_close`).
3. A strategy returns a bracket with stop/target on the wrong side of price, or `qty = 0` → ignored, not filled into a nonsense position. Test in Task 2 (`test_invalid_bracket_is_ignored`).
4. A strategy emits brackets every bar, or several in one bar → only one position at a time. Test in Task 2 (`test_one_position_at_a_time`).
5. Evening-session bars (18:00+ ET belong to the next trading day) → a position entered at 18:02 is not force-closed by the flat-by rule. Test in Task 3 (`test_evening_entry_is_not_force_closed`).

---

### Task 1: Fill model

**Files:**
- Create: `src/fenrir/backtest/__init__.py` (empty)
- Create: `src/fenrir/backtest/fills.py`
- Create: `src/fenrir/backtest/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/backtest/test_fills.py`

**Interfaces:**
- Consumes: `fenrir.core.bars.Bar`, `fenrir.core.actions.Side` (plan 01).
- Produces (`fenrir.backtest.fills`):
  - `OpenPosition(qty: int, entry: float, stop: float, target: float, opened: datetime)` — signed qty, `opened` = entry bar's `ts`.
  - `entry_fill(side: Side, bar: Bar, tick: float) -> float`
  - `market_exit(qty: int, price: float, tick: float) -> float` — `qty` is the position being closed.
  - `bracket_exit(pos: OpenPosition, bar: Bar, tick: float) -> float | None`
  - `gross_pnl(qty: int, entry: float, exit: float, point_value: float) -> float`
  - `worst_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float` — at the bar's adverse extreme.
  - `best_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float` — at the bar's favorable extreme.

- [ ] **Step 1: Write the failing test**

`tests/backtest/test_fills.py`:

```python
from datetime import UTC, datetime

import pytest

from fenrir.backtest.fills import (
    OpenPosition,
    best_open_pnl,
    bracket_exit,
    entry_fill,
    gross_pnl,
    market_exit,
    worst_open_pnl,
)
from fenrir.core.bars import Bar

TS = datetime(2025, 10, 6, 13, 31, tzinfo=UTC)
TICK = 0.25


def b(o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(TS, o, hi, lo, c, 1, "NQZ25")


LONG = OpenPosition(qty=1, entry=100.0, stop=95.0, target=105.0, opened=TS)
SHORT = OpenPosition(qty=-1, entry=100.0, stop=105.0, target=95.0, opened=TS)


def test_entry_pays_one_tick():
    assert entry_fill("long", b(100, 101, 99, 100), TICK) == 100.25
    assert entry_fill("short", b(100, 101, 99, 100), TICK) == 99.75


def test_market_exit_pays_one_tick():
    assert market_exit(2, 100.0, TICK) == 99.75
    assert market_exit(-2, 100.0, TICK) == 100.25


@pytest.mark.parametrize(
    ("pos", "bar", "expected"),
    [
        (LONG, b(100, 104, 96, 101), None),
        (LONG, b(100, 106, 99, 105), 105.0),  # target at the limit
        (LONG, b(100, 101, 94, 95), 94.75),  # stop minus a tick
        (LONG, b(100, 106, 94, 100), 94.75),  # both inside: stop first
        (LONG, b(93, 94, 92, 93), 92.75),  # gap through stop: open minus a tick
        (SHORT, b(100, 104, 96, 101), None),
        (SHORT, b(100, 101, 94, 95), 95.0),
        (SHORT, b(100, 106, 99, 105), 105.25),
        (SHORT, b(100, 106, 94, 100), 105.25),
        (SHORT, b(107, 108, 106, 107), 107.25),
    ],
)
def test_bracket_exit(pos, bar, expected):
    assert bracket_exit(pos, bar, TICK) == expected


def test_pnl():
    assert gross_pnl(1, 100.0, 105.0, 20.0) == 100.0
    assert gross_pnl(-2, 100.0, 105.0, 20.0) == -200.0


def test_worst_open_pnl_uses_adverse_extreme():
    assert worst_open_pnl(LONG, b(100, 104, 97, 101), 20.0) == -60.0
    assert worst_open_pnl(SHORT, b(100, 103, 97, 101), 20.0) == -60.0


def test_best_open_pnl_uses_favorable_extreme():
    assert best_open_pnl(LONG, b(100, 104, 97, 101), 20.0) == 80.0
    assert best_open_pnl(SHORT, b(100, 103, 97, 101), 20.0) == 60.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_fills.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/backtest/__init__.py`: empty file.

`src/fenrir/backtest/fills.py`:

```python
"""Conservative fill model (spec §6). Pure."""

from dataclasses import dataclass
from datetime import datetime

from fenrir.core.actions import Side
from fenrir.core.bars import Bar


@dataclass(frozen=True, slots=True)
class OpenPosition:
    qty: int  # signed contracts
    entry: float
    stop: float
    target: float
    opened: datetime  # ts of the bar the entry filled in


def entry_fill(side: Side, bar: Bar, tick: float) -> float:
    return bar.open + tick if side == "long" else bar.open - tick


def market_exit(qty: int, price: float, tick: float) -> float:
    return price - tick if qty > 0 else price + tick


def bracket_exit(pos: OpenPosition, bar: Bar, tick: float) -> float | None:
    """Exit price if the stop or target trades inside `bar`; the stop wins when both do."""
    if pos.qty > 0:
        if bar.low <= pos.stop:
            return min(pos.stop, bar.open) - tick
        if bar.high >= pos.target:
            return pos.target
    else:
        if bar.high >= pos.stop:
            return max(pos.stop, bar.open) + tick
        if bar.low <= pos.target:
            return pos.target
    return None


def gross_pnl(qty: int, entry: float, exit: float, point_value: float) -> float:
    return (exit - entry) * qty * point_value


def worst_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float:
    return gross_pnl(pos.qty, pos.entry, bar.low if pos.qty > 0 else bar.high, point_value)


def best_open_pnl(pos: OpenPosition, bar: Bar, point_value: float) -> float:
    return gross_pnl(pos.qty, pos.entry, bar.high if pos.qty > 0 else bar.low, point_value)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_fills.py -q`
Expected: `16 passed`

- [ ] **Step 5: Add the directory context file and map row**

`src/fenrir/backtest/CLAUDE.md`:

```markdown
# src/fenrir/backtest/

Backtesting: the fill model, one attempt as a fold over bars, attempts across every trading day, the report JSON, grids and the result cache.

- Entry points: `fills.py` (fill prices), `attempt.py` (`run_attempt` → `AttemptResult`); later `attempts.py`, `report.py`, `run.py` (`run_backtest`), `grid.py`, `cache.py` (plans 09–10).
- Pure vs shell: everything is pure except `run.py` (loads files, process pool) and `cache.py` (reads/writes `data/results/`).
- Fill model is conservative by design (next-bar-open entry +1 tick, stop before target, 1 tick on stops/market exits). Do not make it more optimistic.
- Not here: rule logic (`src/fenrir/rules/`), strategy loading (`src/fenrir/strategy/`), bar storage (`src/fenrir/data/`).
- Tests: `tests/backtest/`.
```

In root `CLAUDE.md`, add this row to the **Directory Map** table:

```markdown
| `src/fenrir/backtest/` | Fill model, single-attempt fold, attempts across days, report JSON, grid expansion and result cache. |
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/backtest/__init__.py src/fenrir/backtest/fills.py src/fenrir/backtest/CLAUDE.md tests/backtest/test_fills.py CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(backtest): add conservative fill model

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Single-attempt fold — orders, fills and breaches

**Files:**
- Create: `src/fenrir/backtest/attempt.py`
- Create: `tests/backtest/backtest_helpers.py`
- Test: `tests/backtest/test_attempt.py`

**Interfaces:**
- Consumes:
  - `fenrir.backtest.fills.*` (Task 1).
  - Plan 01: `Bar`, `ContractSpec`, `Bracket`, `Flatten`, `Cancel`, `Action`, `trading_day`, `NY`.
  - Plan 05: `fenrir.rules.schema.AccountRules` (`rules.evaluation`, `rules.session.flat_by`, `rules.session.tz`, `rules.costs.commission_per_side`).
  - Plan 06: `fenrir.rules.account.apply`, `build_rules`, `initial_state`, `view`; `fenrir.rules.pretrade.check`; `fenrir.rules.state.AccountState` (its `position` field is maintained by the rules from `Fill.qty`; the fold never sets it), `Breach` (`rule` ∈ `"max_drawdown"`, `"daily_loss_limit"`, `"flat_by"`), `Fill`, `Mark(ts, worst_open_pnl, best_open_pnl, close_open_pnl)`, `Rule`, `TradeClosed`.
  - Plan 07: `fenrir.strategy.Strategy`, `Ctx`, `Position`, `FLAT`.
- Produces (`fenrir.backtest.attempt`):
  - `AttemptResult(start: date, outcome: Literal["payout", "passed", "breached", "no_result"], breach: Breach | None, days_to_pass: int | None, days_to_payout: int | None, first_payout: float | None, microscalp_flags: int, fills: tuple[Fill, ...], equity: tuple[tuple[datetime, float], ...])`.
    - `outcome`: `"payout"` reached a first payout; `"passed"` passed evaluation but the data ended before payout; `"breached"`; `"no_result"` data ended during evaluation.
    - `days_to_pass`: trading days from the start day through the passing day (0 for accounts without evaluation). `days_to_payout`: trading days from the start day through the first payout-eligible day.
    - `equity`: one `(last bar ts of the day, balance)` point per completed trading day; `balance` resets to 0 when the funded phase starts.
  - `run_attempt(bars: tuple[Bar, ...], start: int, end: int, strategy: Strategy, rules: AccountRules, spec: ContractSpec) -> AttemptResult` — simulates `bars[start:end]`; `bars[:start]` is history visible in `Ctx.bars` only. Plan 09 calls this.

- [ ] **Step 1: Write the shared test helpers**

`tests/backtest/backtest_helpers.py` (imported by this plan's and plan 09's tests; pytest's default `prepend` import mode puts `tests/backtest/` on `sys.path`):

```python
"""Builders for synthetic backtest inputs. Times are America/New_York wall-clock."""

from datetime import UTC, date, datetime
from pathlib import Path

from fenrir.core.actions import Action
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import NY
from fenrir.rules.schema import AccountRules
from fenrir.strategy import Strategy

NQ = ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0)
D1, D2, D3 = date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8)  # Mon, Tue, Wed


def et(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(UTC)


def bar(hhmm: str, o: float, hi: float, lo: float, c: float, day: date = D1) -> Bar:
    return Bar(et(day, hhmm), o, hi, lo, c, 100, "NQZ25")


def rules_data(
    *,
    target: float = 3000.0,
    drawdown: float = 2000.0,
    daily_loss: float | None = None,
    commission: float = 0.0,
    evaluation: bool = True,
    eval_fee: float = 100.0,
    activation_fee: float = 0.0,
) -> dict:
    """A Lucid-shaped rules dict (spec §4 + plan 05 `locks_at`). Keys must match plan 05's schema."""
    phase = {
        "max_drawdown": {"amount": drawdown, "trailing": "end_of_day", "locks_at": None},
        "daily_loss_limit": daily_loss,
        "consistency": None,
        "min_trading_days": 0,
    }
    return {
        "firm": "lucid",
        "account": "test-50k",
        "verified_on": "2026-10-10",
        "source_url": "https://example.com/rules",
        "automation_allowed": True,
        "evaluation": {**phase, "profit_target": target} if evaluation else None,
        "funded": {
            **phase,
            "payout": {"max_pct_of_profit": 50, "split_pct": 90, "min_days": 0, "min_profit": 0},
        },
        "session": {"flat_by": "16:45", "tz": "America/New_York"},
        "contracts": {"max_minis": 4, "micros_per_mini": 10},
        "prohibited": {"microscalp": {"max_profit_pct": 50, "hold_seconds": 5}, "hft": True},
        "costs": {
            "eval_fee": eval_fee,
            "activation_fee": activation_fee,
            "commission_per_side": {s: commission for s in ("ES", "MES", "NQ", "MNQ")},
        },
    }


def make_rules(**kw) -> AccountRules:
    return AccountRules.model_validate(rules_data(**kw))


def scripted(script: dict[str, list[Action]]) -> Strategy:
    """A strategy that returns `script["HH:MM"]` (ET) on every bar with that time, every day."""

    def step(state, b, ctx):
        return state, list(script.get(b.ts.astimezone(NY).strftime("%H:%M"), []))

    return Strategy("scripted", Path("scripted.py"), "0" * 64, "NQ", {}, lambda p: None, step)
```

If `make_rules()` raises a pydantic `ValidationError`, the keys above have drifted from plan 05's schema: fix `rules_data` to plan 05's field names (the README contract is the source of truth), not the schema.

- [ ] **Step 2: Write the failing tests**

`tests/backtest/test_attempt.py`:

```python
from backtest_helpers import NQ, bar, make_rules, scripted

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket, Cancel, Flatten

LONG = Bracket("long", 1, 95.0, 105.0)
DECIDE = bar("09:30", 100, 100.5, 99.5, 100)  # strategy acts on this bar's close (100)


def attempt(bars, script, **rules_kw):
    return run_attempt(tuple(bars), 0, len(bars), scripted(script), make_rules(**rules_kw), NQ)


def fills(result):
    return [(f.qty, f.price) for f in result.fills]


def test_long_hits_target():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 105.5, 100.5, 105)],
        {"09:30": [LONG]},
    )
    assert fills(r) == [(1, 100.75), (-1, 105.0)]  # entry: next open + 1 tick; target at limit
    assert r.fills[1].realized_pnl == (105.0 - 100.75) * 20  # 85.0
    assert r.fills[0].realized_pnl == 0.0
    assert (r.outcome, r.breach, r.microscalp_flags) == ("no_result", None, 0)


def test_commission_per_contract_per_side():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 105.5, 100.5, 105)],
        {"09:30": [Bracket("long", 2, 95.0, 105.0)]},
        commission=2.5,
    )
    assert [f.commission for f in r.fills] == [5.0, 5.0]


def test_short_hits_target():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 101, 100, 100.5), bar("09:32", 100, 100.5, 94.5, 95.5)],
        {"09:30": [Bracket("short", 1, 105.0, 95.0)]},
    )
    assert fills(r) == [(-1, 100.25), (1, 95.0)]
    assert r.fills[1].realized_pnl == (100.25 - 95.0) * 20  # 105.0


def test_stop_first_when_both_inside_one_bar():
    r = attempt([DECIDE, bar("09:31", 100.5, 106, 94, 100)], {"09:30": [LONG]})
    assert fills(r) == [(1, 100.75), (-1, 94.75)]  # stop 95 - 1 tick, same bar as entry
    assert r.fills[1].realized_pnl == (94.75 - 100.75) * 20  # -120.0
    assert r.microscalp_flags == 1


def test_gap_through_stop_fills_at_open():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 101, 100, 100.5), bar("09:32", 94, 94.5, 93, 94)],
        {"09:30": [LONG]},
    )
    assert fills(r) == [(1, 100.75), (-1, 93.75)]


def test_flatten_exits_at_next_open():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 101.5, 100.5, 101)],
        {"09:30": [LONG], "09:31": [Flatten()]},
    )
    assert fills(r) == [(1, 100.75), (-1, 100.75)]  # 101 open - 1 tick


def test_cancel_drops_pending_entry():
    r = attempt([DECIDE, bar("09:31", 100.5, 102, 100, 101)], {"09:30": [LONG, Cancel("any")]})
    assert r.fills == ()


def test_invalid_bracket_is_ignored():
    bars = [DECIDE, bar("09:31", 100.5, 102, 100, 101)]
    assert attempt(bars, {"09:30": [Bracket("long", 1, 101.0, 105.0)]}).fills == ()  # stop above price
    assert attempt(bars, {"09:30": [Bracket("short", 1, 95.0, 90.0)]}).fills == ()  # stop below price
    assert attempt(bars, {"09:30": [Bracket("long", 0, 95.0, 105.0)]}).fills == ()


def test_one_position_at_a_time():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 105.5, 100.5, 105)],
        {"09:30": [LONG, LONG], "09:31": [LONG]},
    )
    assert len(r.fills) == 2


def test_pretrade_check_rejects_bracket_that_could_breach():
    # stop-out: (100 - 90 + 0.25) * 20 = 205 > 100 drawdown
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101)],
        {"09:30": [Bracket("long", 1, 90.0, 105.0)]},
        drawdown=100.0,
    )
    assert r.fills == ()


def test_drawdown_breach_ends_attempt():
    # pre-trade allows it (stop-out 25), but the gap fills at 93.75: -140 < -100
    r = attempt(
        [
            DECIDE,
            bar("09:31", 100.5, 101, 100, 100.5),
            bar("09:32", 94, 94.5, 93, 94),
            bar("09:33", 94, 99, 94, 98),
        ],
        {"09:30": [Bracket("long", 1, 99.0, 105.0)], "09:33": [LONG]},
        drawdown=100.0,
    )
    assert r.outcome == "breached"
    assert r.breach.rule == "max_drawdown"
    assert fills(r) == [(1, 100.75), (-1, 93.75)]
    assert all(f.ts <= r.breach.ts for f in r.fills)


def test_data_ends_in_position():
    r = attempt([DECIDE, bar("09:31", 100.5, 102, 100, 101)], {"09:30": [LONG]})
    assert fills(r) == [(1, 100.75)]
    assert r.outcome == "no_result"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/backtest/test_attempt.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.backtest.attempt'`

- [ ] **Step 4: Write the fold (without day/phase handling)**

`src/fenrir/backtest/attempt.py`:

```python
"""One attempt: a fold over 1-minute bars until breach, first payout or end of data. Pure."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Literal

from fenrir.backtest.fills import (
    OpenPosition,
    best_open_pnl,
    bracket_exit,
    entry_fill,
    gross_pnl,
    market_exit,
    worst_open_pnl,
)
from fenrir.core.actions import Action, Bracket, Cancel, Flatten
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import trading_day
from fenrir.rules.account import apply, build_rules, initial_state, view
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, Breach, Fill, Mark, Rule, TradeClosed
from fenrir.strategy import FLAT, Ctx, Position, Strategy

WINDOW = 500  # bars of history in Ctx.bars
ADDON_FLATTEN = "16:40"  # the add-on flattens at 16:40 ET on its own (spec §8–9)


@dataclass(frozen=True, slots=True)
class AttemptResult:
    start: date
    outcome: Literal["payout", "passed", "breached", "no_result"]
    breach: Breach | None
    days_to_pass: int | None
    days_to_payout: int | None
    first_payout: float | None
    microscalp_flags: int
    fills: tuple[Fill, ...]
    equity: tuple[tuple[datetime, float], ...]


@dataclass(frozen=True, slots=True)
class _Env:
    bars: tuple[Bar, ...]
    strategy: Strategy
    rules: AccountRules
    spec: ContractSpec
    commission: float  # per contract per side
    flat_at: str  # HH:MM in rules.session.tz
    start: date


@dataclass(frozen=True, slots=True)
class _Sim:
    account: AccountState
    rule_fns: tuple[Rule, ...]
    strat: Any
    position: OpenPosition | None = None
    pending: Bracket | None = None
    exit_requested: bool = False
    days: int = 0
    days_to_pass: int | None = None
    microscalps: int = 0
    fills: tuple[Fill, ...] = ()
    equity: tuple[tuple[datetime, float], ...] = ()


def run_attempt(
    bars: tuple[Bar, ...],
    start: int,
    end: int,
    strategy: Strategy,
    rules: AccountRules,
    spec: ContractSpec,
) -> AttemptResult:
    """Simulate bars[start:end]; bars before `start` are only history for Ctx.bars."""
    day0 = trading_day(bars[start].ts)
    phase = "evaluation" if rules.evaluation is not None else "funded"
    env = _Env(
        bars=bars,
        strategy=strategy,
        rules=rules,
        spec=spec,
        commission=rules.costs.commission_per_side[spec.symbol],
        flat_at=min(rules.session.flat_by, ADDON_FLATTEN),
        start=day0,
    )
    sim = _Sim(
        account=initial_state(rules, phase, day0),
        rule_fns=build_rules(rules, phase),
        strat=strategy.init(strategy.params),
        days_to_pass=None if phase == "evaluation" else 0,
    )
    for i in range(start, end):
        for stage in _STAGES:
            sim = stage(sim, env, i)
            if isinstance(sim, AttemptResult):
                return sim
    return _end(sim, env, "no_result" if sim.days_to_pass is None else "passed")


def _end(sim: _Sim, env: _Env, outcome, breach=None, first_payout=None) -> AttemptResult:
    return AttemptResult(
        start=env.start,
        outcome=outcome,
        breach=breach,
        days_to_pass=sim.days_to_pass,
        days_to_payout=sim.days if outcome == "payout" else None,
        first_payout=first_payout,
        microscalp_flags=sim.microscalps,
        fills=sim.fills,
        equity=sim.equity,
    )


def _event(sim: _Sim, env: _Env, event) -> _Sim | AttemptResult:
    out = apply(sim.rule_fns, sim.account, event)
    if isinstance(out, Breach):
        return _end(sim, env, "breached", breach=out)
    return replace(sim, account=out)


def _open(sim: _Sim, env: _Env, b: Bracket, bar: Bar) -> _Sim | AttemptResult:
    price = entry_fill(b.side, bar, env.spec.tick)
    qty = b.qty if b.side == "long" else -b.qty
    fill = Fill(ts=bar.ts, qty=qty, price=price, commission=env.commission * b.qty, realized_pnl=0.0)
    pos = OpenPosition(qty=qty, entry=price, stop=b.stop, target=b.target, opened=bar.ts)
    return _event(replace(sim, position=pos, pending=None, fills=sim.fills + (fill,)), env, fill)


def _close(sim: _Sim, env: _Env, price: float, ts: datetime) -> _Sim | AttemptResult:
    pos = sim.position
    gross = gross_pnl(pos.qty, pos.entry, price, env.spec.point_value)
    commission = env.commission * abs(pos.qty)
    fill = Fill(ts=ts, qty=-pos.qty, price=price, commission=commission, realized_pnl=gross)
    same_bar = pos.opened == ts
    sim = replace(
        sim, position=None, fills=sim.fills + (fill,), microscalps=sim.microscalps + int(same_bar)
    )
    sim = _event(sim, env, fill)
    if isinstance(sim, AttemptResult):
        return sim
    trade = TradeClosed(opened=pos.opened, closed=ts, pnl=gross - 2 * commission, same_bar=same_bar)
    return _event(sim, env, trade)


def _fill_pending(sim: _Sim, env: _Env, i: int) -> _Sim | AttemptResult:
    bar = env.bars[i]
    if sim.exit_requested and sim.position is not None:
        sim = _close(sim, env, market_exit(sim.position.qty, bar.open, env.spec.tick), bar.ts)
        if isinstance(sim, AttemptResult):
            return sim
    sim = replace(sim, exit_requested=False)
    if sim.pending is None or sim.position is not None:
        return sim
    return _open(sim, env, sim.pending, bar)


def _check_bracket(sim: _Sim, env: _Env, i: int) -> _Sim | AttemptResult:
    if sim.position is None:
        return sim
    bar = env.bars[i]
    price = bracket_exit(sim.position, bar, env.spec.tick)
    return sim if price is None else _close(sim, env, price, bar.ts)


def _mark(sim: _Sim, env: _Env, i: int) -> _Sim | AttemptResult:
    bar, pos = env.bars[i], sim.position
    if pos is None:
        return sim if sim.account.open_pnl == 0 else _event(sim, env, Mark(bar.ts, 0.0, 0.0, 0.0))
    pv = env.spec.point_value
    mark = Mark(
        bar.ts,
        worst_open_pnl(pos, bar, pv),
        best_open_pnl(pos, bar, pv),
        gross_pnl(pos.qty, pos.entry, bar.close, pv),
    )
    return _event(sim, env, mark)


def _position(pos: OpenPosition | None) -> Position:
    return FLAT if pos is None else Position(pos.qty, pos.entry, pos.stop, pos.target)


def _decide(sim: _Sim, env: _Env, i: int) -> _Sim:
    bar = env.bars[i]
    ctx = Ctx(
        ts=bar.ts,
        bars=env.bars[max(0, i - WINDOW + 1) : i + 1],
        position=_position(sim.position),
        account=view(sim.account, env.rules),
        spec=env.spec,
    )
    strat, actions = env.strategy.step(sim.strat, bar, ctx)
    sim = replace(sim, strat=strat)
    late = False  # replaced by the flat-by check in Task 3
    for action in actions:
        sim = _act(sim, env, action, bar.close, late)
    return sim


def _act(sim: _Sim, env: _Env, action: Action, ref: float, late: bool) -> _Sim:
    match action:
        case Flatten():
            return replace(sim, pending=None, exit_requested=sim.position is not None)
        case Cancel():
            return replace(sim, pending=None)
        case Bracket() if (
            not late
            and sim.position is None
            and sim.pending is None
            and _accept(sim, env, action, ref)
        ):
            return replace(sim, pending=action)
    return sim


def _accept(sim: _Sim, env: _Env, b: Bracket, ref: float) -> bool:
    ordered = b.stop < ref < b.target if b.side == "long" else b.target < ref < b.stop
    return b.qty > 0 and ordered and check(sim.account, env.rules, b, env.spec, ref, 0.0) is None


_STAGES = (_fill_pending, _check_bracket, _mark, _decide)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/backtest -q`
Expected: `28 passed` (16 fill + 12 attempt)

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/backtest/attempt.py tests/backtest/backtest_helpers.py tests/backtest/test_attempt.py
git commit -m "$(cat <<'EOF'
feat(backtest): fold one attempt over bars with fills and rules

Entries, brackets, flatten/cancel, pre-trade check and breaches.
Day roll, phases and flat-by follow.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Trading days, flat-by and phases

**Files:**
- Modify: `src/fenrir/backtest/attempt.py`
- Test: `tests/backtest/test_attempt_days.py`

**Interfaces:**
- Consumes: plan 01 `at_or_after(ts, hhmm, tz)` — assumed True iff `ts`'s own trading day has reached `hhmm` (so 18:00–23:59 ET, which belong to the next trading day, return False for `"16:40"`); `trading_day`. Plan 06 `passed`, `payout`, `DayClosed`. The fold emits exactly one `DayClosed` per trading day it saw, after that day's flat-by flatten and before the next day's first event (it is emitted when the next day's first bar arrives, so the last day in the data is never closed). Plan 06's `DayClosed` handlers close `day` but do not set the next one: the fold sets `account.day` to the new trading day with `dataclasses.replace` after applying `DayClosed`.
- Produces: `run_attempt` now closes days, switches evaluation → funded on pass (fresh funded `AccountState`, `days_to_pass` recorded), ends with `"payout"` on the first eligible payout, flattens at flat-by and records `equity`.

- [ ] **Step 1: Write the failing tests**

`tests/backtest/test_attempt_days.py`:

```python
import pytest
from backtest_helpers import D1, D2, D3, NQ, bar, et, make_rules, scripted

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket

LONG = Bracket("long", 1, 95.0, 105.0)


def winning_day(day):
    # entry 100.75 at 09:31, target 105 at 09:32: +85
    return [
        bar("09:30", 100, 100.5, 99.5, 100, day),
        bar("09:31", 100.5, 102, 100, 101, day),
        bar("09:32", 101, 105.5, 100.5, 105, day),
    ]


def attempt(bars, script, **rules_kw):
    return run_attempt(tuple(bars), 0, len(bars), scripted(script), make_rules(**rules_kw), NQ)


def test_pass_then_first_payout():
    bars = [*winning_day(D1), *winning_day(D2), bar("09:30", 100, 100.5, 99.5, 100, D3)]
    r = attempt(bars, {"09:30": [LONG]}, target=50.0)
    assert r.outcome == "payout"
    assert r.days_to_pass == 1
    assert r.days_to_payout == 2
    # plan 06 payout: funded profit 85 x max_pct_of_profit 50% x split 90%
    assert r.first_payout == pytest.approx(85 * 0.50 * 0.90)
    assert r.equity == ((et(D1, "09:32"), 85.0), (et(D2, "09:32"), 85.0))  # funded balance restarts at 0


def test_passed_but_data_ends_before_payout():
    r = attempt([*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)], {}, target=50.0)
    assert r.outcome == "no_result"  # no trades: never passed
    r = attempt([*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)], {"09:30": [LONG]}, target=50.0)
    assert (r.outcome, r.days_to_pass) == ("passed", 1)


def test_account_without_evaluation_starts_funded():
    r = attempt([*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)], {"09:30": [LONG]}, evaluation=False)
    assert (r.outcome, r.days_to_pass, r.days_to_payout) == ("payout", 0, 1)
    assert r.first_payout == pytest.approx(85 * 0.50 * 0.90)


def test_flat_by_forces_exit_and_blocks_entries():
    bars = [
        bar("16:37", 100, 100.5, 99.5, 100),
        bar("16:38", 100, 100.5, 99.5, 100),  # entry 100.25
        bar("16:39", 100, 101, 99.5, 100.5),  # closes 16:40: forced exit 100.5 - tick
        bar("16:40", 100, 100.5, 99.5, 100),
        bar("16:41", 100, 100.5, 99.5, 100),
    ]
    r = attempt(bars, {"16:37": [LONG], "16:39": [LONG], "16:40": [LONG]})
    assert [(f.qty, f.price, f.ts) for f in r.fills] == [
        (1, 100.25, et(D1, "16:38")),
        (-1, 100.25, et(D1, "16:39")),
    ]


def test_evening_entry_is_not_force_closed():
    bars = [
        bar("18:01", 100, 100.5, 99.5, 100),
        bar("18:02", 100, 100.5, 99.5, 100),
        bar("18:03", 100, 100.5, 99.5, 100),
    ]
    r = attempt(bars, {"18:01": [LONG]})
    assert [(f.qty, f.price) for f in r.fills] == [(1, 100.25)]


def test_data_gap_closes_position_before_day_close():
    bars = [
        bar("09:30", 100, 100.5, 99.5, 100),
        bar("09:31", 100.5, 102, 100, 101),  # entry 100.75; data then jumps to the next day
        bar("09:30", 102, 102.5, 101.5, 102, D2),  # exit at open - tick: 101.75
    ]
    r = attempt(bars, {"09:30": [LONG]})
    assert [(f.qty, f.price) for f in r.fills][:2] == [(1, 100.75), (-1, 101.75)]
    assert r.fills[1].ts == et(D2, "09:30")
    assert r.equity == ((et(D1, "09:31"), 20.0),)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/backtest/test_attempt_days.py -q`
Expected: FAIL — `test_pass_then_first_payout` (`outcome == "no_result"`), `test_flat_by_forces_exit_and_blocks_entries` (no forced exit) and `test_data_gap_closes_position_before_day_close` (no exit fill) fail; others may pass by accident.

- [ ] **Step 3: Add day roll, flat-by and phases**

In `src/fenrir/backtest/attempt.py`:

Replace the datetime import line with:

```python
from datetime import date, datetime, timedelta
```

Replace `from fenrir.core.sessions import trading_day` with:

```python
from fenrir.core.sessions import at_or_after, trading_day
```

Replace `from fenrir.rules.account import apply, build_rules, initial_state, view` with:

```python
from fenrir.rules.account import apply, build_rules, initial_state, passed, payout, view
```

Replace `from fenrir.rules.state import AccountState, Breach, Fill, Mark, Rule, TradeClosed` with:

```python
from fenrir.rules.state import AccountState, Breach, DayClosed, Fill, Mark, Rule, TradeClosed
```

Below `ADDON_FLATTEN = ...` add:

```python
MINUTE = timedelta(minutes=1)
```

In `_decide`, replace `late = False  # replaced by the flat-by check in Task 3` with:

```python
    late = _late(bar, env)
```

Replace the `_STAGES = ...` line at the bottom with:

```python
def _late(bar: Bar, env: _Env) -> bool:
    """True once this bar closes at or after the flat-by time of its trading day."""
    return at_or_after(bar.ts + MINUTE, env.flat_at, env.rules.session.tz)


def _flat_time(sim: _Sim, env: _Env, i: int) -> _Sim | AttemptResult:
    bar = env.bars[i]
    if not _late(bar, env):
        return sim
    sim = replace(sim, pending=None)
    if sim.position is None:
        return sim
    return _close(sim, env, market_exit(sim.position.qty, bar.close, env.spec.tick), bar.ts)


def _roll_day(sim: _Sim, env: _Env, i: int) -> _Sim | AttemptResult:
    bar = env.bars[i]
    day = trading_day(bar.ts)
    if day == sim.account.day:
        return sim
    if sim.position is not None:  # data gap skipped flat-by: exit at the first bar we have
        sim = _close(sim, env, market_exit(sim.position.qty, bar.open, env.spec.tick), bar.ts)
        if isinstance(sim, AttemptResult):
            return sim
    closed = DayClosed(day=sim.account.day, ts=bar.ts)
    sim = _event(replace(sim, pending=None, exit_requested=False), env, closed)
    if isinstance(sim, AttemptResult):
        return sim
    sim = replace(
        sim,
        days=sim.days + 1,
        equity=sim.equity + ((env.bars[i - 1].ts, sim.account.balance),),
        account=replace(sim.account, day=day),
    )
    return _phase_check(sim, env, day)


def _phase_check(sim: _Sim, env: _Env, day: date) -> _Sim | AttemptResult:
    if sim.account.phase == "evaluation":
        if not passed(sim.account, env.rules):
            return sim
        return replace(
            sim,
            days_to_pass=sim.days,
            account=initial_state(env.rules, "funded", day),
            rule_fns=build_rules(env.rules, "funded"),
        )
    amount = payout(sim.account, env.rules)
    return sim if amount is None else _end(sim, env, "payout", first_payout=amount)


_STAGES = (_roll_day, _fill_pending, _check_bracket, _flat_time, _mark, _decide)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/backtest -q`
Expected: `34 passed`

If `test_evening_entry_is_not_force_closed` fails, plan 01's `at_or_after` does not treat 18:00+ ET as the next trading day. Fix it in `fenrir.core.sessions` (README contract: "same trading day"), with a test there, not here.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/backtest/attempt.py tests/backtest/test_attempt_days.py
git commit -m "$(cat <<'EOF'
feat(backtest): close trading days, flatten at flat-by, run phases

Evaluation switches to a fresh funded account on pass; the attempt
ends at the first eligible payout. Flat-by is the earlier of the
firm's time and the add-on's 16:40 flatten.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Property tests

**Files:**
- Test: `tests/backtest/test_attempt_properties.py`

**Interfaces:**
- Consumes: `run_attempt`, `AttemptResult` (Tasks 2–3), `backtest_helpers` (Task 2).
- Produces: nothing new; pins spec §12 properties: no trades after a breach, fills never better than the bar allows, same input → same result, never holding across a trading day.

- [ ] **Step 1: Write the property tests**

`tests/backtest/test_attempt_properties.py`:

```python
from datetime import timedelta
from pathlib import Path

from backtest_helpers import D1, NQ, et, make_rules
from hypothesis import given, settings
from hypothesis import strategies as st

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket
from fenrir.core.bars import Bar
from fenrir.core.sessions import NY, trading_day
from fenrir.strategy import Strategy

TICK = NQ.tick
MINUTE = timedelta(minutes=1)


def walk(moves: list[tuple[int, int, int]]) -> tuple[Bar, ...]:
    """Random walk from 14:00 ET, skipping the 17:00 ET maintenance hour (crosses flat-by and a day)."""
    out, ts, price = [], et(D1, "14:00"), 20000.0
    for close_ticks, up, down in moves:
        while ts.astimezone(NY).hour == 17:
            ts += MINUTE
        o, c = price, price + close_ticks * TICK
        out.append(Bar(ts, o, max(o, c) + up * TICK, min(o, c) - down * TICK, c, 1, "NQZ25"))
        price, ts = c, ts + MINUTE
    return tuple(out)


def toy(every: int, stop: int, target: int) -> Strategy:
    """Alternates long/short brackets every `every` bars while flat."""

    def step(n, bar, ctx):
        if n % every or ctx.position.qty:
            return n + 1, []
        sign = 1 if (n // every) % 2 == 0 else -1
        side = "long" if sign == 1 else "short"
        b = Bracket(side, 1, bar.close - sign * stop * TICK, bar.close + sign * target * TICK)
        return n + 1, [b]

    return Strategy("toy", Path("toy.py"), "0" * 64, "NQ", {}, lambda p: 0, step)


inputs = given(
    moves=st.lists(
        st.tuples(st.integers(-8, 8), st.integers(0, 6), st.integers(0, 6)), min_size=2, max_size=400
    ),
    every=st.integers(1, 30),
    stop=st.integers(1, 40),
    target=st.integers(1, 40),
    drawdown=st.sampled_from([150.0, 2000.0]),
)


def run(moves, every, stop, target, drawdown):
    bars = walk(moves)
    result = run_attempt(bars, 0, len(bars), toy(every, stop, target), make_rules(drawdown=drawdown), NQ)
    return bars, result


@inputs
@settings(max_examples=200, deadline=None)
def test_fills_never_better_than_the_bar_allows(moves, every, stop, target, drawdown):
    bars, r = run(moves, every, stop, target, drawdown)
    by_ts = {b.ts: b for b in bars}
    for n, f in enumerate(r.fills):
        b = by_ts[f.ts]
        if f.qty > 0:
            assert f.price >= b.low  # never bought below the bar
        else:
            assert f.price <= b.high  # never sold above the bar
        if n % 2 == 0:  # entries: next open, 1 tick against us
            assert f.price == (b.open + TICK if f.qty > 0 else b.open - TICK)


@inputs
@settings(max_examples=200, deadline=None)
def test_no_fills_after_breach(moves, every, stop, target, drawdown):
    _, r = run(moves, every, stop, target, drawdown)
    if r.breach is not None:
        assert r.outcome == "breached"
        assert all(f.ts <= r.breach.ts for f in r.fills)


@inputs
@settings(max_examples=100, deadline=None)
def test_same_input_same_result(moves, every, stop, target, drawdown):
    assert run(moves, every, stop, target, drawdown)[1] == run(moves, every, stop, target, drawdown)[1]


@inputs
@settings(max_examples=200, deadline=None)
def test_positions_never_span_trading_days(moves, every, stop, target, drawdown):
    _, r = run(moves, every, stop, target, drawdown)
    entries, exits = r.fills[0::2], r.fills[1::2]
    for entry, exit_ in zip(entries, exits, strict=False):
        assert trading_day(entry.ts) == trading_day(exit_.ts)
        assert entry.qty == -exit_.qty
```

- [ ] **Step 2: Run the properties**

Run: `uv run pytest tests/backtest/test_attempt_properties.py -q`
Expected: `4 passed`. A failure prints a minimal falsifying example: treat it as a bug in `attempt.py`/`fills.py` (or in a plan 06 rule it points at), fix the code, and add the example as a golden test in `test_attempt.py` or `test_attempt_days.py`.

- [ ] **Step 3: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all tests pass, `All checks passed!`, nothing to reformat (run `uv run ruff format .` and re-run if needed).

- [ ] **Step 4: Commit**

```bash
git add tests/backtest/test_attempt_properties.py
git commit -m "$(cat <<'EOF'
test(backtest): add property tests for the attempt fold

No fills after a breach, fills never better than the bar, determinism,
no position held across a trading day.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```
