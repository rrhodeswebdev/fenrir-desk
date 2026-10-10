# Rule Functions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Judge a prop-firm account bar by bar: account state, events, one pure function per rule kind, pass and payout checks, the strategy-facing account view, and the pre-trade check shared by the backtester and the live runner.

**Architecture:** Everything here is pure. `state.py` defines the values (`AccountState`, events, `Breach`, `AccountView`). Every rule is a `Rule = (AccountState, Event) -> AccountState | Breach`; `ledger.book` does the accounting and runs first, then each rule kind checks or trails. `account.py` builds an account type's rule tuple from its `AccountRules` and folds events through it with `apply`. Gates that never fail an attempt (consistency, minimum days, payout eligibility) are plain predicates used by `passed` and `payout`. Contract limits are a pre-trade rejection in `pretrade.check`.

**Tech Stack:** Python 3.12, `uv`, `pydantic>=2` (via plan 05's schema), `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§4 Rules library, §5 `ctx`, §8 pre-trade check, §12 Rules tests)

**Index:** `docs/superpowers/plans/README.md` (contract: "Plan 06 — `fenrir.rules.state` / `account` / `pretrade`")

## Global Constraints

- Python 3.12, `uv`. Run tests with `uv run pytest -q`.
- Every value is `@dataclass(frozen=True, slots=True)`; state changes with `dataclasses.replace`. No I/O, clock, randomness or mutation anywhere in this plan.
- Rule signature: `def rule(state: AccountState, event: Event) -> AccountState | Breach`. Each rule kind is implemented once, in its own module.
- Money is in dollars as `float`; `balance` is realized P&L net of commissions since the phase started (starts at `0.0`); equity = `balance + open_pnl`.
- "At or below" the limit is a breach (`<=`), for drawdown and daily loss alike, in rules and in the pre-trade check.
- Times compare in the rules file's `session.tz` (America/New_York); trading days follow CME Globex (plan 01 `trading_day`).
- Commits: Conventional Commits, scope `rules`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A bar that spikes up then crashes on an intraday-trailing account** → the floor trails to the high first, then the low is checked against the new floor (conservative: the order inside a bar is unknown). Test: `test_intraday_trails_on_best_then_checks_worst` (Task 2).
2. **Equity exactly at the floor or exactly at the daily loss limit** → breach, in both the rules and the pre-trade check. Tests: `test_breach_when_worst_equity_touches_floor` (Task 2), `test_breach_at_exact_limit` (Task 3), `test_stop_out_exactly_at_floor_rejected` (Task 7).
3. **Evening session after 18:00 ET** (the next trading day) with a position open → not mistaken for a flat-by violation of the next day. Test: `test_evening_session_is_next_trading_day` (Task 4).
4. **Micro contract sums near the limit** (`0.1` added in floats drifts to `3.0000000000000004`) → 30 micros on a 3-mini account is allowed, 31 is not. Test: `test_micro_float_sums_at_limit` (Task 5).
5. **A buggy strategy's bracket** (stop on the winning side, target on the losing side, `qty` 0) → rejected with a reason before any risk math. Tests: `test_wrong_side_stop_rejected`, `test_wrong_side_target_rejected`, `test_zero_qty_rejected` (Task 7).

## Lucid mechanics to confirm

The schema (plan 05) holds Lucid's numbers; these are the **semantics** this plan assumes. Each one is a single function, so a wrong assumption is a one-file fix. The user should confirm them against Lucid's rules pages before plan 09 reports are trusted.

| Rule | Assumption in this plan | Where |
|---|---|---|
| EOD trailing drawdown | Floor moves only at day close, from end-of-day equity; any intrabar equity at or below the floor breaches. | `drawdown.py` |
| Drawdown lock | `floor = min(peak, locks_at) - amount`; the floor never moves down. | `drawdown.py` |
| Intraday trailing | High-water follows the bar's best unrealized equity; within a bar the high is assumed to come before the low. | `drawdown.py` |
| Daily loss limit | Fails the attempt (hard breach). If Lucid's DLL only stops trading for the day, change it to a day halt. | `daily_loss.py` |
| Consistency | Largest single day ≤ `max_day_pct`% of total net profit over all traded days (today included). It blocks passing or payout; it never fails the attempt. | `consistency.py` |
| Minimum days | Counts days with at least one fill. | `account.py` |
| Pass | Realized `balance >= profit_target`, flat, and the gates above. | `account.py` |
| First payout | `balance × max_pct_of_profit% × split_pct%`, once `min_days`, `min_profit` and consistency hold, while flat. No buffer rule. | `account.py` |
| Microscalp | 1-minute bars cannot show 5-second holds, so same-bar winners are only **flagged** (`possible_microscalp_pnl`); they never breach or block payout. | `microscalp.py` |
| Contract limit | Enforced as a pre-trade rejection, never as an account breach. | `contracts.py`, `pretrade.py` |
| Flat-by | Holding a position on any bar starting at or after `flat_by` breaches; so does a day close while not flat. | `flat_by.py` |

## Contract notes (already in `README.md`)


- `AccountState` gains `position: int` (signed net contracts, summed from `Fill.qty`), placed after `open_pnl`. Flat-by needs it, and it lets a closing fill zero `open_pnl`.
- `Mark` becomes `Mark(ts, worst_open_pnl, best_open_pnl, close_open_pnl)`; `best_open_pnl` is unrealized P&L at the bar's favorable extreme, needed for intraday trailing.
- `Breach.rule` values actually emitted: `"max_drawdown" | "daily_loss_limit" | "flat_by"`. Consistency is a pass/payout gate, contract limits a pre-trade rejection, microscalp a flag.
- Engines (plans 08, 21) emit one `DayClosed` per trading day after the flat-by flatten and before that day's next event, and use keywords when building events.

---

### Task 1: State types and the ledger

**Files:**
- Create: `src/fenrir/rules/state.py`
- Create: `src/fenrir/rules/ledger.py`
- Modify: `tests/rules/conftest.py` (add `make_state` and `et` fixtures)
- Create: `tests/rules/test_ledger.py`

**Interfaces:**
- Consumes: `fenrir.core.sessions.trading_day(ts: datetime) -> date` (plan 01).
- Produces (`fenrir.rules.state`): `Phase`, `AccountState`, `Fill`, `Mark`, `TradeClosed`, `DayClosed`, `Event`, `Breach`, `Rule`, `AccountView` (fields below). (`fenrir.rules.ledger`): `book(state: AccountState, event: Event) -> AccountState` — accounting only, never breaches.

- [ ] **Step 1: Add test fixtures**

Replace `tests/rules/conftest.py` with:

```python
import copy
from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fenrir.rules.state import AccountState

BASE = {
    "firm": "lucid",
    "account": "flex-50k",
    "verified_on": None,
    "source_url": "https://lucidtrading.com",
    "automation_allowed": True,
    "evaluation": {
        "profit_target": 3000,
        "max_drawdown": {"amount": 2000, "trailing": "end_of_day", "locks_at": 2100},
        "daily_loss_limit": None,
        "consistency": None,
        "min_trading_days": 0,
    },
    "funded": {
        "max_drawdown": {"amount": 2000, "trailing": "end_of_day", "locks_at": 2100},
        "daily_loss_limit": None,
        "consistency": None,
        "payout": {"max_pct_of_profit": 50, "split_pct": 90, "min_days": 0, "min_profit": 0},
    },
    "session": {"flat_by": "16:45", "tz": "America/New_York"},
    "contracts": {"max_minis": 4, "micros_per_mini": 10},
    "prohibited": {"microscalp": {"max_profit_pct": 50, "hold_seconds": 5}, "hft": True},
    "costs": {
        "eval_fee": 0,
        "activation_fee": 0,
        "commission_per_side": {"ES": 0, "MES": 0, "NQ": 0, "MNQ": 0},
    },
}

BLANK = AccountState(
    phase="evaluation",
    day=date(2026, 10, 13),
    balance=0.0,
    open_pnl=0.0,
    position=0,
    peak=0.0,
    floor=-2000.0,
    day_start_balance=0.0,
    closed_days=(),
    traded_today=False,
    possible_microscalp_pnl=0.0,
)


@pytest.fixture
def base() -> dict:
    """A fresh, valid account-rules dict; tests mutate it freely."""
    return copy.deepcopy(BASE)


@pytest.fixture
def make_state():
    """make_state(balance=..., ...) -> a flat evaluation state on 2026-10-13 with a -2000 floor."""
    return lambda **changes: replace(BLANK, **changes)


@pytest.fixture
def et():
    """et("16:45", day) -> that New York wall-clock time as a UTC datetime."""

    def at(hhmm: str, day: date = date(2026, 10, 13)) -> datetime:
        h, m = map(int, hhmm.split(":"))
        local = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(timezone.utc)

    return at
```

- [ ] **Step 2: Write the failing tests**

`tests/rules/test_ledger.py`:

```python
from datetime import date

from fenrir.rules.ledger import book
from fenrir.rules.state import DayClosed, Fill, Mark


def test_opening_fill_charges_commission_and_tracks_position(make_state, et):
    s = book(make_state(), Fill(ts=et("10:00"), qty=2, price=20000.0, commission=1.5, realized_pnl=0.0))
    assert (s.balance, s.position, s.traded_today) == (-1.5, 2, True)


def test_closing_fill_realizes_and_clears_open_pnl(make_state, et):
    s = make_state(position=2, open_pnl=300.0)
    s = book(s, Fill(ts=et("10:30"), qty=-2, price=20010.0, commission=1.5, realized_pnl=400.0))
    assert (s.balance, s.position, s.open_pnl) == (398.5, 0, 0.0)


def test_partial_close_keeps_open_pnl_until_next_mark(make_state, et):
    s = make_state(position=2, open_pnl=300.0)
    s = book(s, Fill(ts=et("10:30"), qty=-1, price=20010.0, commission=0.0, realized_pnl=200.0))
    assert (s.position, s.open_pnl) == (1, 300.0)


def test_mark_sets_open_pnl_and_trading_day(make_state, et):
    s = book(make_state(), Mark(ts=et("19:00"), worst_open_pnl=-50.0, best_open_pnl=200.0, close_open_pnl=125.0))
    assert (s.open_pnl, s.day) == (125.0, date(2026, 10, 14))  # 19:00 ET belongs to the next trading day


def test_day_closed_records_traded_day_and_resets(make_state, et):
    s = make_state(balance=500.0, day_start_balance=200.0, traded_today=True)
    s = book(s, DayClosed(day=date(2026, 10, 13), ts=et("17:00")))
    assert s.closed_days == ((date(2026, 10, 13), 300.0),)
    assert (s.day_start_balance, s.traded_today) == (500.0, False)


def test_day_closed_skips_untraded_day(make_state, et):
    s = book(make_state(balance=500.0, day_start_balance=500.0), DayClosed(day=date(2026, 10, 13), ts=et("17:00")))
    assert s.closed_days == ()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.state'` (raised from `conftest.py`)

- [ ] **Step 4: Write the state types**

`src/fenrir/rules/state.py`:

```python
"""Account state, events and breaches shared by every rule function. Pure values only."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Phase = Literal["evaluation", "funded"]


@dataclass(frozen=True, slots=True)
class AccountState:
    phase: Phase
    day: date  # current trading day
    balance: float  # realized P&L net of commissions since phase start
    open_pnl: float  # unrealized P&L at last mark (close)
    position: int  # signed net contracts
    peak: float  # high-water mark the trailing drawdown follows
    floor: float  # equity at or below this = breach
    day_start_balance: float
    closed_days: tuple[tuple[date, float], ...]  # (day, net realized P&L) per completed day with >= 1 fill
    traded_today: bool
    possible_microscalp_pnl: float  # profit from trades opened and closed in the same bar


@dataclass(frozen=True, slots=True)
class Fill:
    ts: datetime
    qty: int  # signed: + buy, - sell
    price: float
    commission: float
    realized_pnl: float  # gross $ realized by this fill (0 for opening fills)


@dataclass(frozen=True, slots=True)
class Mark:
    ts: datetime  # bar open time
    worst_open_pnl: float  # unrealized P&L at the bar's adverse extreme
    best_open_pnl: float  # unrealized P&L at the bar's favorable extreme
    close_open_pnl: float


@dataclass(frozen=True, slots=True)
class TradeClosed:
    opened: datetime
    closed: datetime
    pnl: float
    same_bar: bool


@dataclass(frozen=True, slots=True)
class DayClosed:
    day: date
    ts: datetime


Event = Fill | Mark | TradeClosed | DayClosed


@dataclass(frozen=True, slots=True)
class Breach:
    rule: str  # "max_drawdown" | "daily_loss_limit" | "flat_by"
    reason: str
    ts: datetime


Rule = Callable[[AccountState, Event], AccountState | Breach]


@dataclass(frozen=True, slots=True)
class AccountView:
    phase: Phase
    drawdown_remaining: float  # equity - floor
    daily_loss_remaining: float | None
    today_pnl: float  # realized today + open
    target_remaining: float | None  # None in funded phase
```

- [ ] **Step 5: Write the ledger**

`src/fenrir/rules/ledger.py`:

```python
"""Accounting for every event. Runs before the rules so they see post-event balances."""

from dataclasses import replace

from fenrir.core.sessions import trading_day
from fenrir.rules.state import AccountState, DayClosed, Event, Fill, Mark


def book(state: AccountState, event: Event) -> AccountState:
    match event:
        case Fill():
            position = state.position + event.qty
            return replace(
                state,
                day=trading_day(event.ts),
                balance=state.balance + event.realized_pnl - event.commission,
                position=position,
                open_pnl=state.open_pnl if position else 0.0,
                traded_today=True,
            )
        case Mark():
            return replace(state, day=trading_day(event.ts), open_pnl=event.close_open_pnl)
        case DayClosed():
            today = ((event.day, state.balance - state.day_start_balance),) if state.traded_today else ()
            return replace(
                state,
                closed_days=state.closed_days + today,
                day_start_balance=state.balance,
                traded_today=False,
            )
    return state
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/rules -q`
Expected: PASS — all plan 05 tests plus `6 passed` from `test_ledger.py`

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/fenrir/rules tests/rules && uv run ruff format --check src/fenrir/rules tests/rules`
Expected: `All checks passed!` (run `uv run ruff format src/fenrir/rules tests/rules` if the format check lists files).

- [ ] **Step 8: Commit**

```bash
git add src/fenrir/rules/state.py src/fenrir/rules/ledger.py tests/rules
git commit -m "feat(rules): add account state, events and ledger

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Trailing drawdown (end-of-day, intraday, locking)

**Files:**
- Create: `src/fenrir/rules/drawdown.py`
- Create: `tests/rules/test_drawdown.py`

**Interfaces:**
- Consumes: `AccountState`, `Mark`, `DayClosed`, `Breach`, `Rule` (Task 1); `fenrir.rules.schema.Drawdown` (plan 05).
- Produces: `max_drawdown(dd: Drawdown) -> Rule`. Breach rule name `"max_drawdown"`.

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_drawdown.py`:

```python
from datetime import date

from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.schema import Drawdown
from fenrir.rules.state import Breach, DayClosed, Mark

D = date(2026, 10, 13)


def rule(trailing="end_of_day", locks_at=None):
    return max_drawdown(Drawdown(amount=2000, trailing=trailing, locks_at=locks_at))


def mark(et, worst, best, close, hhmm="10:00"):
    return Mark(ts=et(hhmm), worst_open_pnl=worst, best_open_pnl=best, close_open_pnl=close)


def test_eod_floor_does_not_move_intraday(make_state, et):
    s = rule()(make_state(), mark(et, 0.0, 1500.0, 1500.0))
    assert (s.peak, s.floor) == (0.0, -2000.0)


def test_eod_trails_at_day_close(make_state, et):
    s = rule()(make_state(balance=1500.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (1500.0, -500.0)


def test_floor_never_moves_down(make_state, et):
    s = rule()(make_state(balance=1000.0, peak=1500.0, floor=-500.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (1500.0, -500.0)


def test_breach_when_worst_equity_touches_floor(make_state, et):
    out = rule()(make_state(balance=-1500.0), mark(et, -500.0, 0.0, -100.0))
    assert isinstance(out, Breach) and out.rule == "max_drawdown"


def test_no_breach_one_dollar_above_floor(make_state, et):
    assert not isinstance(rule()(make_state(balance=-1500.0), mark(et, -499.0, 0.0, -100.0)), Breach)


def test_trailing_locks(make_state, et):
    r = rule(locks_at=2100.0)
    s = r(make_state(balance=1000.0), DayClosed(day=D, ts=et("17:00")))
    assert s.floor == -1000.0  # still trailing below the lock
    s = r(make_state(balance=3000.0, peak=1000.0, floor=-1000.0), DayClosed(day=D, ts=et("17:00")))
    assert s.floor == 100.0  # locked at locks_at - amount
    s = r(make_state(balance=5000.0, peak=3000.0, floor=100.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (5000.0, 100.0)


def test_intraday_trails_on_best_then_checks_worst(make_state, et):
    # Up 1500 then down 600 inside one bar: floor trails to -500 first, so -600 breaches.
    out = rule("intraday")(make_state(), mark(et, -600.0, 1500.0, 0.0))
    assert isinstance(out, Breach)


def test_intraday_trails_without_breach(make_state, et):
    s = rule("intraday")(make_state(), mark(et, -100.0, 800.0, 500.0))
    assert (s.peak, s.floor) == (800.0, -1200.0)


def test_intraday_does_not_trail_at_day_close(make_state, et):
    s = rule("intraday")(make_state(balance=1000.0), DayClosed(day=D, ts=et("17:00")))
    assert (s.peak, s.floor) == (0.0, -2000.0)


def test_day_close_breach(make_state, et):
    assert isinstance(rule()(make_state(balance=-2000.0), DayClosed(day=D, ts=et("17:00"))), Breach)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_drawdown.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.drawdown'`

- [ ] **Step 3: Write the rule**

`src/fenrir/rules/drawdown.py`:

```python
"""Trailing max drawdown: end-of-day or intraday, with an optional lock."""

from dataclasses import replace
from datetime import datetime

from fenrir.rules.schema import Drawdown
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Mark, Rule


def max_drawdown(dd: Drawdown) -> Rule:
    def trail(state: AccountState, equity: float) -> AccountState:
        peak = max(state.peak, equity)
        level = peak if dd.locks_at is None else min(peak, dd.locks_at)
        return replace(state, peak=peak, floor=max(state.floor, level - dd.amount))

    def check(state: AccountState, equity: float, ts: datetime) -> AccountState | Breach:
        if equity <= state.floor:
            return Breach("max_drawdown", f"equity {equity:.2f} at or below drawdown floor {state.floor:.2f}", ts)
        return state

    def rule(state: AccountState, event: Event) -> AccountState | Breach:
        match event:
            case Mark():
                if dd.trailing == "intraday":
                    # Order inside a bar is unknown: assume the high came first (conservative).
                    state = trail(state, state.balance + event.best_open_pnl)
                return check(state, state.balance + event.worst_open_pnl, event.ts)
            case DayClosed():
                equity = state.balance + state.open_pnl
                checked = check(state, equity, event.ts)
                if isinstance(checked, Breach) or dd.trailing == "intraday":
                    return checked
                return trail(state, equity)
        return state

    return rule
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_drawdown.py -q`
Expected: PASS — `10 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/rules/drawdown.py tests/rules/test_drawdown.py
git commit -m "feat(rules): add trailing max drawdown rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Daily loss limit

**Files:**
- Create: `src/fenrir/rules/daily_loss.py`
- Create: `tests/rules/test_daily_loss.py`

**Interfaces:**
- Consumes: `AccountState`, `Mark`, `Breach`, `Rule` (Task 1).
- Produces: `daily_loss_limit(limit: float) -> Rule`. Breach rule name `"daily_loss_limit"`. Today's P&L = `balance - day_start_balance + worst_open_pnl`.

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_daily_loss.py`:

```python
from fenrir.rules.daily_loss import daily_loss_limit
from fenrir.rules.state import Breach, Mark


def mark(et, worst):
    return Mark(ts=et("11:00"), worst_open_pnl=worst, best_open_pnl=0.0, close_open_pnl=worst)


def test_breach_at_exact_limit(make_state, et):
    out = daily_loss_limit(1000.0)(make_state(balance=-600.0), mark(et, -400.0))
    assert isinstance(out, Breach) and out.rule == "daily_loss_limit"


def test_no_breach_above_limit(make_state, et):
    assert not isinstance(daily_loss_limit(1000.0)(make_state(balance=-600.0), mark(et, -399.0)), Breach)


def test_yesterdays_loss_not_counted(make_state, et):
    s = make_state(balance=-900.0, day_start_balance=-900.0)
    assert not isinstance(daily_loss_limit(1000.0)(s, mark(et, -100.0)), Breach)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_daily_loss.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.daily_loss'`

- [ ] **Step 3: Write the rule**

`src/fenrir/rules/daily_loss.py`:

```python
"""Daily loss limit: today's realized + worst open P&L must stay above -limit."""

from fenrir.rules.state import AccountState, Breach, Event, Mark, Rule


def daily_loss_limit(limit: float) -> Rule:
    def rule(state: AccountState, event: Event) -> AccountState | Breach:
        if isinstance(event, Mark):
            today = state.balance - state.day_start_balance + event.worst_open_pnl
            if today <= -limit:
                return Breach("daily_loss_limit", f"day P&L {today:.2f} at or below -{limit:.2f}", event.ts)
        return state

    return rule
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_daily_loss.py -q`
Expected: PASS — `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/rules/daily_loss.py tests/rules/test_daily_loss.py
git commit -m "feat(rules): add daily loss limit rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Flat-by time

**Files:**
- Create: `src/fenrir/rules/flat_by.py`
- Create: `tests/rules/test_flat_by.py`

**Interfaces:**
- Consumes: `fenrir.core.sessions.at_or_after(ts, hhmm, tz) -> bool` (plan 01: local wall-clock of `ts` ≥ `hhmm` within the same trading day); `AccountState.position`, `Fill`, `Mark`, `DayClosed`, `Breach`, `Rule` (Task 1).
- Produces: `flat_by(hhmm: str, tz: str) -> Rule`. Breach rule name `"flat_by"`. Engines must flatten before the bar that starts at `flat_by`.

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_flat_by.py`:

```python
from datetime import date

from fenrir.rules.flat_by import flat_by
from fenrir.rules.state import Breach, DayClosed, Fill, Mark

RULE = flat_by("16:45", "America/New_York")


def mark(et, hhmm):
    return Mark(ts=et(hhmm), worst_open_pnl=0.0, best_open_pnl=0.0, close_open_pnl=0.0)


def test_position_before_flat_by_ok(make_state, et):
    assert not isinstance(RULE(make_state(position=1), mark(et, "16:44")), Breach)


def test_position_at_flat_by_breaches(make_state, et):
    out = RULE(make_state(position=1), mark(et, "16:45"))
    assert isinstance(out, Breach) and out.rule == "flat_by"


def test_flat_after_flat_by_ok(make_state, et):
    assert not isinstance(RULE(make_state(), mark(et, "16:50")), Breach)


def test_evening_session_is_next_trading_day(make_state, et):
    assert not isinstance(RULE(make_state(position=1), mark(et, "18:30")), Breach)


def test_opening_fill_after_flat_by_breaches(make_state, et):
    fill = Fill(ts=et("16:46"), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0)
    assert isinstance(RULE(make_state(position=1), fill), Breach)  # rules see post-fill position


def test_day_close_with_position_breaches(make_state, et):
    assert isinstance(RULE(make_state(position=-2), DayClosed(day=date(2026, 10, 13), ts=et("17:00"))), Breach)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_flat_by.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.flat_by'`

- [ ] **Step 3: Write the rule**

`src/fenrir/rules/flat_by.py`:

```python
"""Flat-by: no position may be held at or after the session's flat-by time."""

from fenrir.core.sessions import at_or_after
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Fill, Mark, Rule


def flat_by(hhmm: str, tz: str) -> Rule:
    def rule(state: AccountState, event: Event) -> AccountState | Breach:
        if state.position == 0:
            return state
        match event:
            case Fill() | Mark() if at_or_after(event.ts, hhmm, tz):
                return Breach("flat_by", f"position {state.position} held at or after {hhmm} {tz}", event.ts)
            case DayClosed():
                return Breach("flat_by", f"position {state.position} still open at day close", event.ts)
        return state

    return rule
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_flat_by.py -q`
Expected: PASS — `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/rules/flat_by.py tests/rules/test_flat_by.py
git commit -m "feat(rules): add flat-by time rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Consistency, microscalp flag, contract limit

Three small rule kinds that never fail an attempt on their own: a pass/payout gate, a report flag and a pre-trade limit.

**Files:**
- Create: `src/fenrir/rules/consistency.py`
- Create: `src/fenrir/rules/microscalp.py`
- Create: `src/fenrir/rules/contracts.py`
- Create: `tests/rules/test_small_rules.py`

**Interfaces:**
- Consumes: `AccountState`, `TradeClosed`, `Event` (Task 1); `AccountRules` (plan 05).
- Produces:
  - `consistent(day_pnls: Sequence[float], max_day_pct: float) -> bool`
  - `microscalp(state: AccountState, event: Event) -> AccountState` (a `Rule`; adds same-bar winning trades to `possible_microscalp_pnl`)
  - `contract_limit(rules: AccountRules, held_minis: float, add_minis: float) -> str | None` (reason contains `"contract limit"`)

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_small_rules.py`:

```python
from fenrir.rules.consistency import consistent
from fenrir.rules.contracts import contract_limit
from fenrir.rules.microscalp import microscalp
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import TradeClosed


def test_consistency_broken_by_one_big_day():
    assert not consistent((2500.0, 300.0, 200.0), 40)  # 2500 > 40% of 3000


def test_consistency_holds_when_spread_out():
    assert consistent((1000.0, 900.0, 800.0, 700.0), 40)  # 1000 <= 40% of 3400


def test_losing_days_reduce_the_total():
    assert not consistent((1200.0, -200.0, 800.0, 700.0), 40)  # 1200 > 40% of 2500


def test_no_profit_is_not_consistent():
    assert not consistent((), 40)
    assert not consistent((-100.0, 50.0), 40)


def trade(et, pnl, same_bar):
    return TradeClosed(opened=et("10:00"), closed=et("10:00"), pnl=pnl, same_bar=same_bar)


def test_same_bar_winner_is_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, 50.0, True)).possible_microscalp_pnl == 50.0


def test_same_bar_loser_not_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, -50.0, True)).possible_microscalp_pnl == 0.0


def test_multi_bar_winner_not_flagged(make_state, et):
    assert microscalp(make_state(), trade(et, 50.0, False)).possible_microscalp_pnl == 0.0


def test_contract_limit_with_micros(base):
    rules = AccountRules.model_validate(base)  # max_minis 4
    assert contract_limit(rules, 3.5, 5 * 0.1) is None
    assert "contract limit" in contract_limit(rules, 3.5, 6 * 0.1)


def test_micro_float_sums_at_limit(base):
    base["contracts"]["max_minis"] = 3
    rules = AccountRules.model_validate(base)
    assert 0.1 + 29 * 0.1 > 3  # 3.0000000000000004 in floats
    assert contract_limit(rules, 0.1, 29 * 0.1) is None  # 30 micros = 3 minis: allowed
    assert contract_limit(rules, 0.1, 30 * 0.1) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_small_rules.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.consistency'`

- [ ] **Step 3: Write the three functions**

`src/fenrir/rules/consistency.py`:

```python
"""Consistency: no single day may exceed max_day_pct of total profit."""

from collections.abc import Sequence


def consistent(day_pnls: Sequence[float], max_day_pct: float) -> bool:
    total = sum(day_pnls)
    return total > 0 and max(day_pnls) <= total * max_day_pct / 100
```

`src/fenrir/rules/microscalp.py`:

```python
"""Possible microscalps: 1-minute bars can't show hold time, so same-bar winners are flagged, never breached."""

from dataclasses import replace

from fenrir.rules.state import AccountState, Event, TradeClosed


def microscalp(state: AccountState, event: Event) -> AccountState:
    if isinstance(event, TradeClosed) and event.same_bar and event.pnl > 0:
        return replace(state, possible_microscalp_pnl=state.possible_microscalp_pnl + event.pnl)
    return state
```

`src/fenrir/rules/contracts.py`:

```python
"""Contract limit in mini-equivalents (a micro counts as ContractSpec.minis, 0.1)."""

from fenrir.rules.schema import AccountRules


def contract_limit(rules: AccountRules, held_minis: float, add_minis: float) -> str | None:
    total = round(held_minis + add_minis, 6)  # 0.1 steps drift in floats
    if total > rules.contracts.max_minis:
        return f"contract limit: {total:g} mini-equivalents exceeds max_minis {rules.contracts.max_minis}"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_small_rules.py -q`
Expected: PASS — `9 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/rules/consistency.py src/fenrir/rules/microscalp.py src/fenrir/rules/contracts.py tests/rules/test_small_rules.py
git commit -m "feat(rules): add consistency, microscalp flag and contract limit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Account type — build, apply, pass, payout, view

**Files:**
- Create: `src/fenrir/rules/account.py`
- Create: `tests/rules/test_account.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5; `AccountRules`, `schema.Phase` (plan 05).
- Produces (`fenrir.rules.account`), exactly as in the index:
  - `initial_state(rules: AccountRules, phase: Phase, day: date) -> AccountState` — `ValueError` for `"evaluation"` on an account with `evaluation: null`
  - `build_rules(rules: AccountRules, phase: Phase) -> tuple[Rule, ...]` — `(book, max_drawdown, flat_by[, daily_loss_limit][, microscalp])`, in that order
  - `apply(rule_fns: tuple[Rule, ...], state: AccountState, event: Event) -> AccountState | Breach` — first `Breach` wins
  - `passed(state: AccountState, rules: AccountRules) -> bool`
  - `payout(state: AccountState, rules: AccountRules) -> float | None`
  - `view(state: AccountState, rules: AccountRules) -> AccountView`
  - also `phase_rules(rules, phase) -> schema.Phase` and `day_pnls(state) -> tuple[float, ...]` (used by `pretrade`)

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_account.py`:

```python
from datetime import date

import pytest

from fenrir.rules.account import apply, build_rules, initial_state, passed, payout, view
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import Breach, DayClosed, Fill, Mark

D1, D2, D3, D4 = (date(2026, 10, d) for d in (13, 14, 15, 16))


def trade_day(et, day, pnl):
    return (
        Fill(ts=et("10:00", day), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0),
        Fill(ts=et("10:30", day), qty=-1, price=20000.0 + pnl / 20, commission=0.0, realized_pnl=pnl),
        DayClosed(day=day, ts=et("17:00", day)),
    )


def run(rules, phase, events):
    fns = build_rules(rules, phase)
    state = initial_state(rules, phase, D1)
    for event in events:
        state = apply(fns, state, event)
        if isinstance(state, Breach):
            break
    return state


def test_initial_state(base):
    s = initial_state(AccountRules.model_validate(base), "evaluation", D1)
    assert (s.phase, s.balance, s.floor, s.position) == ("evaluation", 0.0, -2000.0, 0)


def test_direct_has_no_evaluation(base):
    base["evaluation"] = None
    rules = AccountRules.model_validate(base)
    with pytest.raises(ValueError, match="no evaluation"):
        initial_state(rules, "evaluation", D1)
    assert initial_state(rules, "funded", D1).phase == "funded"


def test_build_rules_includes_only_set_kinds(base):
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 4
    base["evaluation"]["daily_loss_limit"] = 1000
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 5
    base["prohibited"]["microscalp"] = None
    assert len(build_rules(AccountRules.model_validate(base), "evaluation")) == 4


def test_passes_when_target_met(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0))
    assert s.balance == 3100.0 and passed(s, rules)


def test_pass_needs_flat(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 3100.0))
    s = apply(build_rules(rules, "evaluation"), s, Fill(ts=et("10:00", D2), qty=1, price=1.0, commission=0.0, realized_pnl=0.0))
    assert not passed(s, rules)


def test_min_trading_days(base, et):
    base["evaluation"]["min_trading_days"] = 3
    rules = AccountRules.model_validate(base)
    s = run(rules, "evaluation", trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0))
    assert not passed(s, rules)
    s = run(rules, "evaluation", trade_day(et, D1, 1800.0) + trade_day(et, D2, 1300.0) + trade_day(et, D3, 100.0))
    assert passed(s, rules)


def test_consistency_blocks_pass_until_profit_spreads(base, et):
    base["evaluation"]["consistency"] = {"max_day_pct": 40}
    rules = AccountRules.model_validate(base)
    two = trade_day(et, D1, 2500.0) + trade_day(et, D2, 600.0)
    assert not passed(run(rules, "evaluation", two), rules)  # 2500 > 40% of 3100
    four = two + trade_day(et, D3, 1500.0) + trade_day(et, D4, 1700.0)
    assert passed(run(rules, "evaluation", four), rules)  # 2500 <= 40% of 6300


def test_first_payout_amount(base, et):
    rules = AccountRules.model_validate(base)
    s = run(rules, "funded", trade_day(et, D1, 2000.0))
    assert payout(s, rules) == 900.0  # 2000 x 50% x 90%


def test_payout_waits_for_min_days(base, et):
    base["funded"]["payout"]["min_days"] = 5
    rules = AccountRules.model_validate(base)
    assert payout(run(rules, "funded", trade_day(et, D1, 2000.0)), rules) is None


def test_no_payout_on_loss_or_in_evaluation(base, et):
    rules = AccountRules.model_validate(base)
    assert payout(run(rules, "funded", trade_day(et, D1, -500.0)), rules) is None
    assert payout(run(rules, "evaluation", trade_day(et, D1, 2000.0)), rules) is None


def test_view(base, make_state):
    base["evaluation"]["daily_loss_limit"] = 1000
    rules = AccountRules.model_validate(base)
    v = view(make_state(balance=1000.0, open_pnl=-200.0, day_start_balance=600.0, floor=-1000.0), rules)
    assert (v.drawdown_remaining, v.today_pnl, v.daily_loss_remaining, v.target_remaining) == (1800.0, 200.0, 1200.0, 2000.0)
    assert view(make_state(phase="funded"), rules).target_remaining is None


def test_first_breach_wins_and_stops(base, et):
    base["evaluation"]["daily_loss_limit"] = 500
    rules = AccountRules.model_validate(base)
    events = (
        Fill(ts=et("10:00"), qty=1, price=20000.0, commission=0.0, realized_pnl=0.0),
        Mark(ts=et("10:00"), worst_open_pnl=-2500.0, best_open_pnl=0.0, close_open_pnl=-2400.0),
    )
    out = run(rules, "evaluation", events)
    assert isinstance(out, Breach) and out.rule == "max_drawdown"  # drawdown runs before daily loss
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_account.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.account'`

- [ ] **Step 3: Write the account module**

`src/fenrir/rules/account.py`:

```python
"""An account type = its rule functions built from YAML, plus pass/payout gates and the strategy view."""

from datetime import date

from fenrir.rules import schema
from fenrir.rules.consistency import consistent
from fenrir.rules.daily_loss import daily_loss_limit
from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.flat_by import flat_by
from fenrir.rules.ledger import book
from fenrir.rules.microscalp import microscalp
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, AccountView, Breach, Event, Phase, Rule


def phase_rules(rules: AccountRules, phase: Phase) -> schema.Phase:
    p = rules.evaluation if phase == "evaluation" else rules.funded
    if p is None:
        raise ValueError(f"{rules.firm}-{rules.account} has no evaluation phase")
    return p


def initial_state(rules: AccountRules, phase: Phase, day: date) -> AccountState:
    return AccountState(
        phase=phase,
        day=day,
        balance=0.0,
        open_pnl=0.0,
        position=0,
        peak=0.0,
        floor=-phase_rules(rules, phase).max_drawdown.amount,
        day_start_balance=0.0,
        closed_days=(),
        traded_today=False,
        possible_microscalp_pnl=0.0,
    )


def build_rules(rules: AccountRules, phase: Phase) -> tuple[Rule, ...]:
    p = phase_rules(rules, phase)
    dll = (daily_loss_limit(p.daily_loss_limit),) if p.daily_loss_limit is not None else ()
    scalp = (microscalp,) if rules.prohibited.microscalp is not None else ()
    return (book, max_drawdown(p.max_drawdown), flat_by(rules.session.flat_by, rules.session.tz), *dll, *scalp)


def apply(rule_fns: tuple[Rule, ...], state: AccountState, event: Event) -> AccountState | Breach:
    for fn in rule_fns:
        result = fn(state, event)
        if isinstance(result, Breach):
            return result
        state = result
    return state


def day_pnls(state: AccountState) -> tuple[float, ...]:
    today = (state.balance - state.day_start_balance,) if state.traded_today else ()
    return tuple(pnl for _, pnl in state.closed_days) + today


def _consistent(p: schema.Phase, days: tuple[float, ...]) -> bool:
    return p.consistency is None or consistent(days, p.consistency.max_day_pct)


def passed(state: AccountState, rules: AccountRules) -> bool:
    e = rules.evaluation
    if state.phase != "evaluation" or e is None or state.position != 0:
        return False
    days = day_pnls(state)
    return state.balance >= e.profit_target and len(days) >= e.min_trading_days and _consistent(e, days)


def payout(state: AccountState, rules: AccountRules) -> float | None:
    f = rules.funded
    p = f.payout  # never None: the schema requires it on funded
    if state.phase != "funded" or state.position != 0 or state.balance <= 0:
        return None
    days = day_pnls(state)
    if state.balance < p.min_profit or len(days) < p.min_days or not _consistent(f, days):
        return None
    return state.balance * p.max_pct_of_profit / 100 * p.split_pct / 100


def view(state: AccountState, rules: AccountRules) -> AccountView:
    p = phase_rules(rules, state.phase)
    today = state.balance - state.day_start_balance + state.open_pnl
    return AccountView(
        phase=state.phase,
        drawdown_remaining=state.balance + state.open_pnl - state.floor,
        daily_loss_remaining=None if p.daily_loss_limit is None else p.daily_loss_limit + today,
        today_pnl=today,
        target_remaining=None if p.profit_target is None else max(0.0, p.profit_target - state.balance),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_account.py -q`
Expected: PASS — `12 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/rules/account.py tests/rules/test_account.py
git commit -m "feat(rules): build account types and add pass, payout and view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Pre-trade check

**Files:**
- Create: `src/fenrir/rules/pretrade.py`
- Create: `tests/rules/test_pretrade.py`
- Modify: `src/fenrir/rules/CLAUDE.md`

**Interfaces:**
- Consumes: `fenrir.core.actions.Bracket`, `fenrir.core.instruments.ContractSpec` (plan 01); `phase_rules` (Task 6); `contract_limit` (Task 5).
- Produces: `check(state: AccountState, rules: AccountRules, bracket: Bracket, spec: ContractSpec, entry_price: float, held_minis: float) -> str | None`. Stop-out loss = `(|entry - stop| + 1 tick) × point_value × qty + 2 × commission_per_side[symbol] × qty`. Rejects when `balance + open_pnl - loss <= floor`, when today's P&L after the loss `<= -daily_loss_limit`, on the contract limit, or on a malformed bracket. Used by plan 08 (backtest) and plan 21 (live); the live safety margin is plan 21's, not this.

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_pretrade.py`:

```python
import pytest

from fenrir.core.actions import Bracket
from fenrir.core.instruments import ContractSpec
from fenrir.rules.pretrade import check
from fenrir.rules.schema import AccountRules

NQ = ContractSpec(symbol="NQ", series="NQ", point_value=20.0, tick=0.25, minis=1.0)
MNQ = ContractSpec(symbol="MNQ", series="NQ", point_value=2.0, tick=0.25, minis=0.1)


def long(stop, target=20100.0, qty=1):
    return Bracket(side="long", qty=qty, stop=stop, target=target)


def test_accepts_affordable_bracket(base, make_state):
    assert check(make_state(), AccountRules.model_validate(base), long(19980.0), NQ, 20000.0, 0.0) is None


def test_rejects_stop_out_through_floor(base, make_state):
    reason = check(make_state(), AccountRules.model_validate(base), long(19900.0), NQ, 20000.0, 0.0)
    assert "drawdown" in reason  # (100 + 0.25) x 20 = 2005 > 2000


def test_stop_out_exactly_at_floor_rejected(base, make_state):
    assert check(make_state(), AccountRules.model_validate(base), long(19900.25), NQ, 20000.0, 0.0) is not None  # loss 2000


def test_rejects_stop_out_through_daily_loss(base, make_state):
    base["evaluation"]["daily_loss_limit"] = 500
    reason = check(make_state(), AccountRules.model_validate(base), long(19975.0), NQ, 20000.0, 0.0)
    assert "daily loss" in reason  # 25.25 x 20 = 505


def test_round_trip_commission_counts(base, make_state):
    s, b = make_state(balance=-7.0), long(19900.75)  # stop-out 1990 before commission
    assert check(s, AccountRules.model_validate(base), b, NQ, 20000.0, 0.0) is None
    base["costs"]["commission_per_side"]["NQ"] = 2.0
    assert "drawdown" in check(s, AccountRules.model_validate(base), b, NQ, 20000.0, 0.0)


def test_contract_limit_with_micros(base, make_state):
    rules = AccountRules.model_validate(base)
    assert check(make_state(), rules, long(19990.0, qty=5), MNQ, 20000.0, 3.5) is None
    assert "contract limit" in check(make_state(), rules, long(19990.0, qty=6), MNQ, 20000.0, 3.5)


@pytest.mark.parametrize(("side", "stop", "target"), [("long", 20010.0, 20100.0), ("short", 19990.0, 19900.0)])
def test_wrong_side_stop_rejected(base, make_state, side, stop, target):
    b = Bracket(side=side, qty=1, stop=stop, target=target)
    assert "stop" in check(make_state(), AccountRules.model_validate(base), b, NQ, 20000.0, 0.0)


def test_wrong_side_target_rejected(base, make_state):
    b = Bracket(side="short", qty=1, stop=20020.0, target=20010.0)
    assert "target" in check(make_state(), AccountRules.model_validate(base), b, NQ, 20000.0, 0.0)


def test_zero_qty_rejected(base, make_state):
    assert "qty" in check(make_state(), AccountRules.model_validate(base), long(19980.0, qty=0), NQ, 20000.0, 0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_pretrade.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.pretrade'`

- [ ] **Step 3: Write the check**

`src/fenrir/rules/pretrade.py`:

```python
"""Reject a bracket whose stop-out would breach a limit. Shared by backtest and live."""

from fenrir.core.actions import Bracket
from fenrir.core.instruments import ContractSpec
from fenrir.rules.account import phase_rules
from fenrir.rules.contracts import contract_limit
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState


def check(
    state: AccountState,
    rules: AccountRules,
    bracket: Bracket,
    spec: ContractSpec,
    entry_price: float,
    held_minis: float,
) -> str | None:
    if bracket.qty <= 0:
        return f"qty must be positive, got {bracket.qty}"
    sign = 1 if bracket.side == "long" else -1
    if sign * (entry_price - bracket.stop) <= 0:
        return f"{bracket.side} stop {bracket.stop} is not on the losing side of entry {entry_price}"
    if sign * (bracket.target - entry_price) <= 0:
        return f"{bracket.side} target {bracket.target} is not on the winning side of entry {entry_price}"
    if reason := contract_limit(rules, held_minis, bracket.qty * spec.minis):
        return reason
    loss = (abs(entry_price - bracket.stop) + spec.tick) * spec.point_value * bracket.qty + (
        2 * rules.costs.commission_per_side[spec.symbol] * bracket.qty
    )
    equity_after = state.balance + state.open_pnl - loss
    if equity_after <= state.floor:
        return f"stop-out loss {loss:.2f} would put equity {equity_after:.2f} at or below drawdown floor {state.floor:.2f}"
    limit = phase_rules(rules, state.phase).daily_loss_limit
    today_after = state.balance - state.day_start_balance + state.open_pnl - loss
    if limit is not None and today_after <= -limit:
        return f"stop-out loss {loss:.2f} would put day P&L {today_after:.2f} at or below daily loss limit -{limit:.2f}"
    return None
```

- [ ] **Step 4: Run the whole rules suite**

Run: `uv run pytest tests/rules -q`
Expected: PASS — plan 05's 28 tests plus 6 + 10 + 3 + 6 + 9 + 12 + 10 = 56 from this plan (`84 passed` if plan 05 shipped only `flex-50k.yaml`; one more per extra rules file).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/fenrir/rules tests/rules && uv run ruff format --check src/fenrir/rules tests/rules`
Expected: `All checks passed!`

- [ ] **Step 6: Update the directory context file**

Replace `src/fenrir/rules/CLAUDE.md` with:

```markdown
# src/fenrir/rules/

Prop-firm account rules: the YAML schema and loader, account state, one pure function per rule kind, pass/payout gates and the pre-trade check.

- Entry points: `account.py` (`initial_state`, `build_rules`, `apply`, `passed`, `payout`, `view`), `pretrade.py` (`check`), `state.py` (`AccountState`, events, `Breach`, `AccountView`), `loader.py` (`load_account`, `list_accounts`, `rules_path`, `dump_account`), `schema.py` (`AccountRules`).
- One rule kind per module: `drawdown.py`, `daily_loss.py`, `flat_by.py`, `consistency.py`, `microscalp.py`, `contracts.py`; `ledger.book` does the accounting and runs first.
- Pure vs shell: everything is pure except `loader.py` (reads files).
- Lucid mechanics this code assumes are listed in `docs/superpowers/plans/2026-10-10-06-rule-functions.md` ("Lucid mechanics to confirm"). Change one module per wrong assumption.
- Not here: the YAML files (`rules/lucid/`), fills and the attempt fold (`src/fenrir/backtest/`), the live safety margin (`src/fenrir/live/`).
- Tests: `tests/rules/` (`conftest.py` has `base`, `make_state`, `et`).
```

- [ ] **Step 7: Commit**

```bash
git add src/fenrir/rules/pretrade.py src/fenrir/rules/CLAUDE.md tests/rules/test_pretrade.py
git commit -m "feat(rules): add pre-trade check for stop-out limits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
