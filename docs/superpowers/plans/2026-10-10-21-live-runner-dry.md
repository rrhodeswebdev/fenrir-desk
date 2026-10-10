# Live Runner (Dry Run) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run each armed strategy on live LSE bars through the whole decision pipeline (step → rules → pre-trade check) and journal every bar, action, rejection, alert and halt, without sending any order.

**Architecture:** `fenrir.live.core` is a pure state machine: `on_bar(RunnerState, Bar) -> (RunnerState, effects)` and `on_tick(RunnerState, now, stale_seconds) -> (RunnerState, effects)`. In this plan the only effect is `Log(kind, payload)`. `fenrir.live.service.LiveHub` is the imperative shell: it reads armed accounts from SQLite on every tick and every closed bar, feeds bars and clock ticks into the core, and writes effects to the journal. `fenrir serve` starts the hub through a FastAPI lifespan. `GET /api/live/status` and `GET /api/live/journal` expose it. Plan 22 adds order sending on top of the same core.

**Tech Stack:** Python 3.12, sqlite3 (stdlib), FastAPI, pytest, pytest-asyncio.

**Spec:** docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md (§7 Arming is user-only, §8 Modes, Loop, Failure handling, Trade journal)

**Index:** docs/superpowers/plans/README.md (contracts: plans 01, 04, 05, 06, 07, 11, "Plans 21–24 — live API and storage")

> **PROVISIONAL (gate):** Task 4's call to `fenrir.data.live.stream_bars` and Task 6's manual check depend on the real LSE client. Before executing, read `docs/data-gate.md` (plan 02) and confirm `stream_bars(settings, contracts)` exists with the plan 04 signature; adjust Task 4 Step 3 if it does not.

## Global Constraints

- Functional core, imperative shell: `core.py` is pure (no I/O, clock, randomness); `service.py`, `journal.py`, `arming.py` and `routes/live.py` are the shell.
- No LLM in the live loop; plain strategy code decides trades (spec §1).
- One armed strategy per account; different accounts may run different strategies.
- Dry run: live LSE bars, full pipeline, actions logged but **not sent** (spec §8 Modes).
- Pre-trade check rejects any bracket whose stop-out would breach the daily loss limit, the drawdown limit or the contract limit, and logs the rejection (spec §8 step 4).
- Limits use the stricter of NinjaTrader's figures and the app's own; in dry run only the app's own exist.
- Safety margin: default 80% of the daily loss limit used → halt that account for the rest of the day (`Settings.safety_margin_pct`).
- No LSE bar for `Settings.stale_seconds` in session → pause new entries and alert.
- No new entries at or after 16:40 America/New_York (the add-on flattens then).
- An account whose strategy file hash on disk differs from `armed.strategy_hash` is halted (checked on every closed bar).
- No CLI command arms or disarms; `armed` rows are written only through `fenrir.live.arming` (spec §7).
- Journal: SQLite `data/fenrir.db`, opened with `fenrir.live.journal.open_db`, tables `journal` and `armed`.
- Server listens on 127.0.0.1 only; every `/api` route requires the session token.
- Commits: Conventional Commits, scope `live`.

## Review Focus

1. **The strategy's `step` raises** → that account halts with the error journaled; the runner and other accounts keep going. Test: `test_strategy_error_halts_account` (Task 2).
2. **The strategy file is edited while armed** → the account halts on the next bar and stays halted until re-armed. Tests: `test_changed_strategy_file_halts_at_start` (Task 2), `test_file_edited_while_armed_halts_on_next_bar` (Task 4).
3. **LSE goes quiet mid-session, then resumes** → entries pause with one alert and resume on the next bar; nights, the 17:00–18:00 break and weekends never raise it. Tests: `test_stale_data_pauses_then_resumes`, `test_no_stale_alert_outside_session` (Task 3).
4. **A day-scoped halt (safety margin) lifts on the next trading day; a breach never does.** Tests: `test_day_halt_lifts_next_trading_day`, `test_breach_halt_survives_day_roll` (Task 3).
5. **A broken armed row (missing strategy file, unknown rules account)** → one alert, no crash, no alert on every tick. Test: `test_broken_armed_row_alerts_once` (Task 4).

---

### Task 1: Journal and armed storage

**Files:**
- Create: `src/fenrir/live/__init__.py`
- Create: `src/fenrir/live/journal.py`
- Create: `src/fenrir/live/arming.py`
- Create: `src/fenrir/live/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/live/test_journal.py`

**Interfaces:**
- Consumes: `fenrir.core.sessions.NY` (plan 01).
- Produces (used by plans 22, 23, 24):

```python
# fenrir.live.journal
KINDS = frozenset({"bar", "action", "rejection", "fill", "alert", "halt"})
@dataclass(frozen=True, slots=True)
class JournalRow: id: int; ts: datetime; account: str; kind: str; payload: dict
def open_db(path: Path) -> sqlite3.Connection          # creates both tables; parent dir too
def write(conn, ts: datetime, account: str, kind: str, payload: Mapping[str, Any]) -> None
def rows(conn, account: str | None = None, day: date | None = None) -> tuple[JournalRow, ...]   # ascending id
def day_bounds(day: date) -> tuple[datetime, datetime]  # UTC [18:00 ET day-1, 17:00 ET day)

# fenrir.live.arming  (storage only; plan 23 adds the arm/disarm checks and endpoints on top)
@dataclass(frozen=True, slots=True)
class Armed:
    account: str; strategy_path: str; strategy_hash: str; rules_account: str
    contract: str; mode: str          # "dry" | "sim" | "live"
    phase: str                        # "evaluation" | "funded"
    armed_at: datetime
def list_armed(conn) -> tuple[Armed, ...]               # sorted by account
def insert_armed(conn, a: Armed) -> None                # insert or replace (one strategy per account)
def delete_armed(conn, account: str) -> None
```

`armed.phase` tells the runner which phase's rules apply (index: "Plans 21–24 — live API and storage").

- [ ] **Step 1: Write the failing tests**

`tests/live/test_journal.py`:

```python
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from fenrir.live import arming, journal
from fenrir.live.arming import Armed


@pytest.fixture
def conn(tmp_path):
    return journal.open_db(tmp_path / "data" / "fenrir.db")


def test_write_and_read_back(conn):
    ts = datetime(2026, 10, 12, 13, 31, tzinfo=UTC)
    journal.write(conn, ts, "Sim101", "action", {"type": "Bracket", "qty": 1})
    [row] = journal.rows(conn)
    assert (row.ts, row.account, row.kind, row.payload) == (ts, "Sim101", "action", {"type": "Bracket", "qty": 1})


def test_unknown_kind_is_rejected(conn):
    with pytest.raises(ValueError, match="kind"):
        journal.write(conn, datetime.now(UTC), "Sim101", "chatter", {})


def test_rows_filter_by_account_and_trading_day(conn):
    # trading day 2026-10-12 runs 2026-10-11 18:00 ET (22:00 UTC) to 2026-10-12 17:00 ET (21:00 UTC)
    journal.write(conn, datetime(2026, 10, 11, 21, 59, tzinfo=UTC), "Sim101", "bar", {"n": 0})
    journal.write(conn, datetime(2026, 10, 11, 22, 0, tzinfo=UTC), "Sim101", "bar", {"n": 1})
    journal.write(conn, datetime(2026, 10, 12, 20, 59, tzinfo=UTC), "Sim101", "bar", {"n": 2})
    journal.write(conn, datetime(2026, 10, 12, 21, 0, tzinfo=UTC), "Sim101", "bar", {"n": 3})
    journal.write(conn, datetime(2026, 10, 12, 14, 0, tzinfo=UTC), "Other", "bar", {"n": 4})
    got = journal.rows(conn, account="Sim101", day=date(2026, 10, 12))
    assert [r.payload["n"] for r in got] == [1, 2]


def test_rearming_replaces_and_delete_removes(conn):
    a = Armed("Sim101", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "dry", "evaluation",
              datetime(2026, 10, 12, 13, 0, tzinfo=UTC))
    arming.insert_armed(conn, a)
    arming.insert_armed(conn, replace(a, strategy_hash="h2", mode="sim"))
    assert arming.list_armed(conn) == (replace(a, strategy_hash="h2", mode="sim"),)
    arming.delete_armed(conn, "Sim101")
    assert arming.list_armed(conn) == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_journal.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.live'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/live/__init__.py`: empty file.

`src/fenrir/live/journal.py`:

```python
"""SQLite trade journal. Shell: every function does I/O on the connection."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fenrir.core.sessions import NY

KINDS = frozenset({"bar", "action", "rejection", "fill", "alert", "halt"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_account_ts ON journal (account, ts);
CREATE TABLE IF NOT EXISTS armed (
    account TEXT PRIMARY KEY,
    strategy_path TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    rules_account TEXT NOT NULL,
    contract TEXT NOT NULL,
    mode TEXT NOT NULL,
    phase TEXT NOT NULL,
    armed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class JournalRow:
    id: int
    ts: datetime
    account: str
    kind: str
    payload: dict


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()  # one format everywhere, so string comparison orders by time


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(_SCHEMA)
    return conn


def write(conn: sqlite3.Connection, ts: datetime, account: str, kind: str, payload: Mapping[str, Any]) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind {kind!r}")
    with conn:
        conn.execute(
            "INSERT INTO journal (ts, account, kind, payload_json) VALUES (?, ?, ?, ?)",
            (iso(ts), account, kind, json.dumps(payload, default=str)),
        )


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day - timedelta(days=1), time(18), NY)
    end = datetime.combine(day, time(17), NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def rows(conn: sqlite3.Connection, account: str | None = None, day: date | None = None) -> tuple[JournalRow, ...]:
    sql, args = "SELECT id, ts, account, kind, payload_json FROM journal WHERE 1=1", []
    if account is not None:
        sql += " AND account = ?"
        args.append(account)
    if day is not None:
        start, end = day_bounds(day)
        sql += " AND ts >= ? AND ts < ?"
        args += [iso(start), iso(end)]
    return tuple(
        JournalRow(i, datetime.fromisoformat(ts), acc, kind, json.loads(p))
        for i, ts, acc, kind, p in conn.execute(sql + " ORDER BY id", args)
    )
```

`src/fenrir/live/arming.py`:

```python
"""Armed-strategy storage. The only writer of the `armed` table; plan 23 adds the arm/disarm checks."""

import sqlite3
from dataclasses import astuple, dataclass
from datetime import datetime

from fenrir.live.journal import iso


@dataclass(frozen=True, slots=True)
class Armed:
    account: str
    strategy_path: str
    strategy_hash: str
    rules_account: str
    contract: str
    mode: str
    phase: str
    armed_at: datetime


def list_armed(conn: sqlite3.Connection) -> tuple[Armed, ...]:
    return tuple(
        Armed(*r[:7], datetime.fromisoformat(r[7]))
        for r in conn.execute(
            "SELECT account, strategy_path, strategy_hash, rules_account, contract, mode, phase, armed_at"
            " FROM armed ORDER BY account"
        )
    )


def insert_armed(conn: sqlite3.Connection, a: Armed) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO armed VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*astuple(a)[:7], iso(a.armed_at)))


def delete_armed(conn: sqlite3.Connection, account: str) -> None:
    with conn:
        conn.execute("DELETE FROM armed WHERE account = ?", (account,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/live/test_journal.py -q`
Expected: `4 passed`

- [ ] **Step 5: Add the directory context file and map row**

`src/fenrir/live/CLAUDE.md`:

```markdown
# src/fenrir/live/

Runs armed strategies on live bars: decides, checks limits, journals, and (from plan 22) sends orders through the bridge.

- Entry points: `core.py` (`start`, `on_bar`, `on_tick`, `account_status`: the pure runner), `service.py` (`LiveHub`, `live_lifespan`, `load_setup`: the shell), `journal.py` (`open_db`, journal rows), `arming.py` (`Armed`, `list_armed`, `insert_armed`, `delete_armed`).
- Pure vs shell: `core.py` is pure `(RunnerState, input) -> (RunnerState, effects)`; `service.py`, `journal.py`, `arming.py` do all I/O.
- Not here: the arm/disarm endpoints (`src/fenrir/server/routes/arming.py`, plan 23); the add-on client (`src/fenrir/bridge/`); rule math (`src/fenrir/rules/`).
- Never add a CLI command that arms or disarms, and never write the `armed` table outside `arming.py` (spec §7).
- Tests: `tests/live/`.
```

In root `CLAUDE.md`, add this row to the `## 6. Directory Map` table:

```markdown
| `src/fenrir/live/` | Live runner: pure per-bar decisions, limits and halts, SQLite trade journal and armed table, server lifecycle. |
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/live/__init__.py src/fenrir/live/journal.py src/fenrir/live/arming.py src/fenrir/live/CLAUDE.md tests/live/test_journal.py CLAUDE.md
git commit -m "feat(live): add sqlite trade journal and armed storage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Runner core — per-bar decisions

**Files:**
- Create: `src/fenrir/live/core.py`
- Create: `src/fenrir/live/service.py` (only `load_setup`; Task 4 adds the hub)
- Create: `tests/live/fixtures.py`, `tests/live/conftest.py`
- Test: `tests/live/test_runner_core.py`

**Interfaces:**
- Consumes: `Bar` (01), `Action`, `Bracket` (01), `ContractSpec`, `Instruments`, `Roll`, `spec` (01), `trading_day`, `at_or_after` (01), `Settings` (01), `load_account`, `AccountRules` (05), `AccountState`, `Mark(ts, worst_open_pnl, best_open_pnl, close_open_pnl)`, `DayClosed`, `Breach`, `Event`, `Rule` (06), `fenrir.rules.account.initial_state/build_rules/apply/view` (06), `fenrir.rules.pretrade.check` (06), `Ctx`, `Position`, `Strategy` (07), `fenrir.strategy.loader.load_strategy/file_hash` (07), `Armed` (Task 1).
- Produces (plan 22 extends these in place):

```python
@dataclass(frozen=True, slots=True)
class Setup: armed: Armed; strategy: Strategy; rules: AccountRules; rule_fns: tuple[Rule, ...]; spec: ContractSpec; safety_margin_pct: float
@dataclass(frozen=True, slots=True)
class RunnerState:
    setup: Setup; strat: Any; acct: AccountState; position: Position; bars: tuple[Bar, ...]
    last_seen: datetime                   # close time of the last bar, or start time
    halted: str | None = None             # no new entries while set
    halt_day: date | None = None          # halt lifts after this trading day; None = until disarmed
    paused: frozenset[str] = frozenset()  # no new entries while non-empty
    breach: Breach | None = None          # account failed; rules are no longer applied
@dataclass(frozen=True, slots=True)
class Log: kind: str; payload: Mapping[str, Any]
Effect = Log
def start(setup: Setup, now: datetime) -> tuple[RunnerState, tuple[Effect, ...]]
def on_bar(s: RunnerState, bar: Bar) -> tuple[RunnerState, tuple[Effect, ...]]
def halt(s: RunnerState, reason: str) -> tuple[RunnerState, tuple[Effect, ...]]   # open-ended halt (until disarmed)
def account_status(s: RunnerState) -> dict[str, Any]   # one AccountStatus (index "Plans 21–24")
# fenrir.live.service
def load_setup(a: Armed, settings: Settings, instruments: Instruments) -> Setup
```

`Ctx`, `Position` and `Strategy` are imported from `fenrir.strategy`. If plan 07 does not re-export them from the package `__init__`, import them from the module plan 07 defines them in.

- [ ] **Step 1: Write the shared test fixtures**

`tests/live/fixtures.py` (plain constants and helpers; pytest's default `prepend` import mode puts `tests/live/` on `sys.path`, so tests import it as `fixtures`):

```python
from datetime import UTC, date, datetime

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec, Instruments, Roll

RULES_YAML = """\
firm: lucid
account: test-50k
verified_on: 2026-10-10
source_url: https://example.com/rules
automation_allowed: true
evaluation:
  profit_target: 3000
  max_drawdown: { amount: 2000, trailing: end_of_day, locks_at: null }
  daily_loss_limit: 1000
  consistency: null
  min_trading_days: 0
funded:
  max_drawdown: { amount: 2000, trailing: end_of_day, locks_at: null }
  daily_loss_limit: 1000
  consistency: null
  payout: { max_pct_of_profit: 50, split_pct: 90, min_days: 0, min_profit: 0 }
session: { flat_by: "16:45", tz: America/New_York }
contracts: { max_minis: 4, micros_per_mini: 10 }
prohibited:
  microscalp: { max_profit_pct: 50, hold_seconds: 5 }
  hft: true
costs: { eval_fee: 0, activation_fee: 0, commission_per_side: { ES: 0, MES: 0, NQ: 0, MNQ: 0 } }
"""

STRATEGY = '''\
from fenrir.core.actions import Bracket

MARKET = "NQ"
PARAMS = {"qty": 1}


def init(params):
    return params["qty"]


def step(state, bar, ctx):
    if ctx.position.qty == 0:
        return state, [Bracket("long", state, bar.close - 10, bar.close + 20)]
    return state, []
'''

NQ = ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0)
INSTRUMENTS = Instruments(
    specs=(ContractSpec("ES", "ES", 50.0, 0.25, 1.0), ContractSpec("MES", "ES", 5.0, 0.25, 0.1),
           NQ, ContractSpec("MNQ", "NQ", 2.0, 0.25, 0.1)),
    rolls=(Roll("ES", date(2026, 9, 14), "ESZ26"), Roll("NQ", date(2026, 9, 14), "NQZ26")),
)
T0 = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)  # Monday 09:30 ET
ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)


def bar(ts, o=20000.0, h=20005.0, lo=19995.0, c=20000.0, contract="NQZ26"):
    return Bar(ts=ts, open=o, high=h, low=lo, close=c, volume=100, contract=contract)
```

`tests/live/conftest.py`:

```python
import pytest

from fenrir.live.arming import Armed
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash
from fixtures import ARMED_AT, RULES_YAML, STRATEGY


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "rules" / "lucid").mkdir(parents=True)
    (tmp_path / "rules" / "lucid" / "test-50k.yaml").write_text(RULES_YAML)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "always_long.py").write_text(STRATEGY)
    return Settings(data_dir=tmp_path / "data", rules_dir=tmp_path / "rules", strategies_dir=tmp_path / "strategies")


@pytest.fixture
def armed(settings):
    path = settings.strategies_dir / "always_long.py"
    return Armed("Sim101", str(path), file_hash(path), "lucid-test-50k", "NQ", "dry", "evaluation", ARMED_AT)
```

- [ ] **Step 2: Write the failing tests**

`tests/live/test_runner_core.py`:

```python
from dataclasses import replace
from datetime import timedelta

import pytest

from fenrir.live import core
from fenrir.live.service import load_setup
from fixtures import INSTRUMENTS, T0, bar


@pytest.fixture
def setup(armed, settings):
    return load_setup(armed, settings, INSTRUMENTS)


@pytest.fixture
def state(setup):
    s, effects = core.start(setup, T0)
    assert effects == ()
    return s


def kinds(effects):
    return [e.kind for e in effects]


def test_dry_bar_logs_bar_and_unsent_bracket(state):
    s, effects = core.on_bar(state, bar(T0))
    assert kinds(effects) == ["bar", "action"]
    action = effects[1].payload
    assert (action["type"], action["side"], action["qty"], action["sent"], action["mode"]) == (
        "Bracket", "long", 1, False, "dry")
    assert s.position.qty == 0  # dry run never fills
    assert s.bars[-1] == bar(T0)


def test_bars_window_is_capped(state):
    s = state
    for i in range(core.MAX_BARS + 5):
        s, _ = core.on_bar(s, bar(T0 + timedelta(minutes=i % 300)))
    assert len(s.bars) == core.MAX_BARS


def test_over_contract_limit_is_rejected(setup):
    big = replace(setup, strategy=replace(setup.strategy, params={"qty": 5}))
    s, _ = core.start(big, T0)
    _, effects = core.on_bar(s, bar(T0))
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["action"]["qty"] == 5


def test_no_entries_at_or_after_1640(state):
    _, effects = core.on_bar(state, bar(T0.replace(hour=20, minute=39)))  # closes 16:40 ET
    assert kinds(effects) == ["bar", "rejection"]
    assert "16:40" in effects[1].payload["reason"]


def test_halted_account_rejects(state):
    s, effects = core.halt(state, "test")
    assert kinds(effects) == ["halt"]
    _, effects = core.on_bar(s, bar(T0))
    assert effects[1].kind == "rejection"
    assert effects[1].payload["reason"] == "halted: test"


def test_strategy_error_halts_account(setup):
    def boom(state, bar, ctx):
        raise RuntimeError("bad index")

    s, _ = core.start(replace(setup, strategy=replace(setup.strategy, step=boom)), T0)
    s, effects = core.on_bar(s, bar(T0))
    assert kinds(effects) == ["bar", "halt"]
    assert "bad index" in s.halted and s.halt_day is None


def test_changed_strategy_file_halts_at_start(setup):
    s, effects = core.start(replace(setup, armed=replace(setup.armed, strategy_hash="old")), T0)
    assert kinds(effects) == ["halt"]
    assert "changed since arming" in s.halted
    _, effects = core.on_bar(s, bar(T0))
    assert effects[1].kind == "rejection"


def test_account_status_shape(state):
    st = core.account_status(state)
    assert set(st) == {"account", "mode", "strategy", "rules_account", "contract", "halted", "halt_reason",
                       "balance", "limits", "position"}
    assert set(st["limits"]) == {"drawdown_remaining", "daily_loss_remaining", "max_minis", "held_minis"}
    assert st["halted"] is False and st["position"]["qty"] == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_runner_core.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.live.core'`

- [ ] **Step 4: Write the core**

`src/fenrir/live/core.py`:

```python
"""Pure live-runner core: (RunnerState, input) -> (RunnerState, effects). No I/O, clock or randomness."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from fenrir.core.actions import Action, Bracket
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import at_or_after, trading_day
from fenrir.live.arming import Armed
from fenrir.rules import account as acct_rules
from fenrir.rules import pretrade
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Mark, Rule
from fenrir.strategy import Ctx, Position, Strategy

MAX_BARS = 500
ADDON_FLATTEN = "16:40"  # the add-on flattens everything at this time; never open a position at or after it
FLAT = Position(qty=0, entry=None, stop=None, target=None)


@dataclass(frozen=True, slots=True)
class Setup:
    armed: Armed
    strategy: Strategy
    rules: AccountRules
    rule_fns: tuple[Rule, ...]
    spec: ContractSpec
    safety_margin_pct: float


@dataclass(frozen=True, slots=True)
class RunnerState:
    setup: Setup
    strat: Any
    acct: AccountState
    position: Position
    bars: tuple[Bar, ...]
    last_seen: datetime
    halted: str | None = None
    halt_day: date | None = None
    paused: frozenset[str] = frozenset()
    breach: Breach | None = None


@dataclass(frozen=True, slots=True)
class Log:
    kind: str
    payload: Mapping[str, Any]


Effect = Log
Step = tuple[RunnerState, tuple[Effect, ...]]


def start(setup: Setup, now: datetime) -> Step:
    s = RunnerState(
        setup=setup,
        strat=setup.strategy.init(setup.strategy.params),
        acct=acct_rules.initial_state(setup.rules, setup.armed.phase, trading_day(now)),
        position=FLAT,
        bars=(),
        last_seen=now,
    )
    if setup.strategy.hash != setup.armed.strategy_hash:
        return halt(s, "strategy file changed since arming; re-run the backtest and re-arm")
    return s, ()


def halt(s: RunnerState, reason: str) -> Step:
    return _halt(s, reason, None)


def on_bar(s: RunnerState, bar: Bar) -> Step:
    s, out = _roll_day(s, bar.ts)
    bars = (*s.bars, bar)[-MAX_BARS:]
    s = replace(s, bars=bars, last_seen=bar.ts + timedelta(minutes=1))
    s, more = _apply(s, _mark(s.position, bar, s.setup.spec))
    out += more
    s, more = _safety(s)
    out += more
    ctx = Ctx(ts=bar.ts, bars=bars, position=s.position,
              account=acct_rules.view(s.acct, s.setup.rules), spec=s.setup.spec)
    try:
        strat, actions = s.setup.strategy.step(s.strat, bar, ctx)
    except Exception as e:  # a strategy bug must stop this account, not the runner
        s, more = halt(s, f"strategy error: {e!r}")
        return s, (*out, Log("bar", _bar_payload(bar, 0)), *more)
    s = replace(s, strat=strat)
    out += (Log("bar", _bar_payload(bar, len(actions))),)
    for i, a in enumerate(actions):
        s, more = _decide(s, a, bar, i)
        out += more
    return s, out


def account_status(s: RunnerState) -> dict[str, Any]:
    a = s.setup.armed
    v = acct_rules.view(_limits_acct(s), s.setup.rules)
    reasons = ([s.halted] if s.halted else []) + sorted(s.paused)
    return {
        "account": a.account,
        "mode": a.mode,
        "strategy": s.setup.strategy.name,
        "rules_account": a.rules_account,
        "contract": a.contract,
        "halted": bool(reasons),
        "halt_reason": "; ".join(reasons) or None,
        "balance": s.acct.balance,
        "limits": {
            "drawdown_remaining": v.drawdown_remaining,
            "daily_loss_remaining": v.daily_loss_remaining,
            "max_minis": s.setup.rules.contracts.max_minis,
            "held_minis": abs(s.position.qty) * s.setup.spec.minis,
        },
        "position": asdict(s.position),
    }


def _bar_payload(bar: Bar, n_actions: int) -> dict[str, Any]:
    return {**asdict(bar), "actions": n_actions}


def _action_payload(a: Action) -> dict[str, Any]:
    return {"type": type(a).__name__, **asdict(a)}


def _mark(pos: Position, bar: Bar, spec: ContractSpec) -> Mark:
    if pos.qty == 0 or pos.entry is None:
        return Mark(bar.ts, 0.0, 0.0, 0.0)
    worst, best = (bar.low, bar.high) if pos.qty > 0 else (bar.high, bar.low)
    per_point = pos.qty * spec.point_value
    return Mark(bar.ts, (worst - pos.entry) * per_point, (best - pos.entry) * per_point,
                (bar.close - pos.entry) * per_point)


def _apply(s: RunnerState, event: Event) -> Step:
    if s.breach:
        return s, ()
    r = acct_rules.apply(s.setup.rule_fns, s.acct, event)
    if isinstance(r, Breach):
        s, out = _halt(s, f"breach {r.rule}: {r.reason}", None)
        return replace(s, breach=r), out
    return replace(s, acct=r), ()


def _halt(s: RunnerState, reason: str, until: date | None) -> Step:
    if s.halted == reason or (s.halted and s.halt_day is None):
        return s, ()  # an open-ended halt is never replaced or downgraded to a day halt
    payload = {"reason": reason, "until": until.isoformat() if until else None}
    return replace(s, halted=reason, halt_day=until), (Log("halt", payload),)


def _phase_rules(s: RunnerState):
    r = s.setup.rules
    return r.funded if s.setup.armed.phase == "funded" else r.evaluation


def _limits_acct(s: RunnerState) -> AccountState:
    return s.acct  # dry run: only our own figures exist


def _safety(s: RunnerState) -> Step:
    dll = _phase_rules(s).daily_loss_limit
    if dll is None:
        return s, ()
    a = _limits_acct(s)
    lost = -(a.balance - a.day_start_balance + a.open_pnl)
    if lost < dll * s.setup.safety_margin_pct / 100:
        return s, ()
    return _halt(s, f"safety margin: lost ${lost:,.0f} of ${dll:,.0f} daily loss limit", s.acct.day)


def _roll_day(s: RunnerState, ts: datetime) -> Step:
    day = trading_day(ts)
    if day <= s.acct.day:
        return s, ()
    s, out = _apply(s, DayClosed(s.acct.day, ts))  # exactly one per trading day, before the new day's first event
    s = replace(s, acct=replace(s.acct, day=day, day_start_balance=s.acct.balance, traded_today=False))
    if s.halted and s.halt_day is not None and day > s.halt_day:
        out += (Log("alert", {"message": f"halt lifted: {s.halted}"}),)
        s = replace(s, halted=None, halt_day=None)
    return s, out


def _block(s: RunnerState, b: Bracket, bar: Bar) -> str | None:
    if s.halted:
        return f"halted: {s.halted}"
    if s.paused:
        return "paused: " + ", ".join(sorted(s.paused))
    if s.position.qty != 0:
        return "already in a position"
    if at_or_after(bar.ts + timedelta(minutes=1), ADDON_FLATTEN):
        return "at or after 16:40 ET add-on flatten"
    held = abs(s.position.qty) * s.setup.spec.minis
    return pretrade.check(_limits_acct(s), s.setup.rules, b, s.setup.spec, entry_price=bar.close, held_minis=held)


def _decide(s: RunnerState, a: Action, bar: Bar, i: int) -> Step:
    if isinstance(a, Bracket):
        reason = _block(s, a, bar)
        if reason:
            return s, (Log("rejection", {"action": _action_payload(a), "reason": reason}),)
    return _accept(s, a, bar, i)


def _accept(s: RunnerState, a: Action, bar: Bar, i: int) -> Step:
    return s, (Log("action", {**_action_payload(a), "mode": s.setup.armed.mode, "sent": False}),)
```

`src/fenrir/live/service.py`:

```python
"""Live runner shell: loads armed accounts, feeds bars and ticks into the core, performs effects."""

from pathlib import Path

from fenrir.core.instruments import Instruments, spec
from fenrir.live.arming import Armed
from fenrir.live.core import Setup
from fenrir.rules.account import build_rules
from fenrir.rules.loader import load_account
from fenrir.settings import Settings
from fenrir.strategy.loader import load_strategy


def load_setup(a: Armed, settings: Settings, instruments: Instruments) -> Setup:
    rules = load_account(a.rules_account, settings.rules_dir)
    return Setup(
        armed=a,
        strategy=load_strategy(Path(a.strategy_path)),
        rules=rules,
        rule_fns=build_rules(rules, a.phase),
        spec=spec(instruments, a.contract),
        safety_margin_pct=settings.safety_margin_pct,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/live -q`
Expected: `12 passed` (4 journal + 8 core)

If `test_over_contract_limit_is_rejected` fails because `pretrade.check` returns `None`, 5 NQ against `max_minis: 4` must be rejected per the index contract: fix plan 06's code, not this test.

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/live/core.py src/fenrir/live/service.py tests/live/fixtures.py tests/live/conftest.py tests/live/test_runner_core.py
git commit -m "feat(live): add pure runner core for per-bar decisions in dry run

Runs step, applies rules to a mark, checks safety margin and pre-trade
limits, and logs bar, action, rejection and halt effects.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Day roll, halts and stale data

**Files:**
- Modify: `src/fenrir/live/core.py` (add `STALE`, `in_session`, `on_tick`; clear `STALE` in `on_bar`)
- Test: `tests/live/test_runner_time.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:

```python
STALE = "stale_data"
def in_session(now: datetime) -> bool        # CME Globex: Sun 18:00 – Fri 17:00 ET, daily break 17:00–18:00
def on_tick(s: RunnerState, now: datetime, stale_seconds: int) -> tuple[RunnerState, tuple[Effect, ...]]
```

- [ ] **Step 1: Write the failing tests**

`tests/live/test_runner_time.py`:

```python
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fenrir.live import core
from fenrir.live.service import load_setup
from fenrir.rules.state import Breach
from fixtures import INSTRUMENTS, T0, bar


@pytest.fixture
def state(armed, settings):
    s, _ = core.start(load_setup(armed, settings, INSTRUMENTS), T0)
    return s


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 12, 13, 30, tzinfo=UTC), True),   # Mon 09:30 ET
        (datetime(2026, 10, 12, 21, 30, tzinfo=UTC), False),  # Mon 17:30 ET daily break
        (datetime(2026, 10, 12, 22, 0, tzinfo=UTC), True),    # Mon 18:00 ET reopen
        (datetime(2026, 10, 16, 21, 0, tzinfo=UTC), False),   # Fri 17:00 ET close
        (datetime(2026, 10, 17, 15, 0, tzinfo=UTC), False),   # Saturday
        (datetime(2026, 10, 18, 21, 59, tzinfo=UTC), False),  # Sun 17:59 ET
        (datetime(2026, 10, 18, 22, 0, tzinfo=UTC), True),    # Sun 18:00 ET open
    ],
)
def test_in_session(utc, expected):
    assert core.in_session(utc) is expected


def test_stale_data_pauses_then_resumes(state):
    s, effects = core.on_tick(state, T0 + timedelta(seconds=60), 90)
    assert effects == () and s.paused == frozenset()
    s, effects = core.on_tick(s, T0 + timedelta(seconds=91), 90)
    assert [e.kind for e in effects] == ["alert"] and s.paused == {core.STALE}
    s, effects = core.on_tick(s, T0 + timedelta(seconds=120), 90)
    assert effects == ()  # one alert, not one per tick
    _, effects = core.on_bar(s, bar(T0 + timedelta(minutes=2)))
    assert [e.kind for e in effects] == ["bar", "action"]  # cleared by the bar; entries allowed again


def test_no_stale_alert_outside_session(state):
    saturday = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
    s, effects = core.on_tick(replace(state, last_seen=saturday - timedelta(hours=20)), saturday, 90)
    assert effects == () and not s.paused


def test_day_halt_lifts_next_trading_day(state):
    s = replace(state, halted="safety margin", halt_day=state.acct.day)
    s, _ = core.on_bar(s, bar(T0 + timedelta(hours=1)))
    assert s.halted == "safety margin"
    s, effects = core.on_bar(s, bar(T0 + timedelta(days=1)))
    assert s.halted is None
    assert "alert" in [e.kind for e in effects]


def test_breach_halt_survives_day_roll(state):
    s = replace(state, halted="breach max_drawdown: x", halt_day=None, breach=Breach("max_drawdown", "x", T0))
    s, _ = core.on_bar(s, bar(T0 + timedelta(days=1)))
    assert s.halted == "breach max_drawdown: x"


def test_safety_margin_halts_for_the_day(state):
    s = replace(state, acct=replace(state.acct, balance=-850.0, day_start_balance=0.0))
    s, effects = core.on_bar(s, bar(T0))
    assert [e.kind for e in effects] == ["halt", "bar", "rejection"]
    assert s.halted.startswith("safety margin") and s.halt_day == state.acct.day
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_runner_time.py -q`
Expected: FAIL with `AttributeError: module 'fenrir.live.core' has no attribute 'in_session'` (the halt and safety tests may already pass).

- [ ] **Step 3: Implement**

In `src/fenrir/live/core.py`:

Change the sessions import to:

```python
from fenrir.core.sessions import NY, at_or_after, trading_day
```

Add after `FLAT = ...`:

```python
STALE = "stale_data"
```

In `on_bar`, replace

```python
    s = replace(s, bars=bars, last_seen=bar.ts + timedelta(minutes=1))
```

with

```python
    s = replace(s, bars=bars, last_seen=bar.ts + timedelta(minutes=1), paused=s.paused - {STALE})
```

Append to the end of the file:

```python
def in_session(now: datetime) -> bool:
    # shortcut: CME holidays count as in session, so they raise a stale-data pause; add a holiday calendar if noisy
    t = now.astimezone(NY)
    hm = (t.hour, t.minute)
    if t.weekday() == 5:
        return False
    if t.weekday() == 6:
        return hm >= (18, 0)
    if t.weekday() == 4:
        return hm < (17, 0)
    return not ((17, 0) <= hm < (18, 0))


def on_tick(s: RunnerState, now: datetime, stale_seconds: int) -> Step:
    window = timedelta(seconds=stale_seconds + 60)  # whole window in session, so the reopen is not "stale"
    if STALE in s.paused or not (in_session(now) and in_session(now - window)):
        return s, ()
    age = (now - s.last_seen).total_seconds()
    if age <= stale_seconds:
        return s, ()
    msg = f"no {s.setup.strategy.market} bar for {age:.0f}s; new entries paused"
    return replace(s, paused=s.paused | {STALE}), (Log("alert", {"message": msg}),)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/live -q`
Expected: `26 passed` (4 journal + 8 core + 14 time)

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/live/core.py tests/live/test_runner_time.py
git commit -m "feat(live): pause entries on stale data and lift day halts on day roll

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: LiveHub shell and server lifecycle

**Files:**
- Modify: `src/fenrir/live/service.py` (add `LiveHub`, `run_ticks`, `run_bars`, `live_lifespan`)
- Modify: `src/fenrir/server/app.py` (`create_app`: hub + lifespan)
- Test: `tests/live/test_hub.py`

**Interfaces:**
- Consumes: `journal`, `arming` (Task 1), `core` (Tasks 2–3), `load_instruments`, `front_month` (01), `file_hash` (07), `stream_bars` (04, PROVISIONAL), `create_app` (11; settings on `app.state.settings`).
- Produces (plan 22 extends; plans 23–24 read `app.state.live`):

```python
class LiveHub:
    settings: Settings; instruments: Instruments; conn: sqlite3.Connection
    states: dict[str, RunnerState]; broken: dict[str, tuple[Armed, str]]
    lse_connected: bool; last_bar_ts: datetime | None
    def __init__(self, settings: Settings, instruments: Instruments | None = None) -> None
    def sync(self, now: datetime) -> None          # list_armed → states (start new, drop disarmed/changed)
    def tick(self, now: datetime) -> None          # sync + stale checks
    def handle_bar(self, bar: Bar) -> None         # sync, halt edited files, route bar to its accounts
    def perform(self, account: str, effects: tuple[Effect, ...]) -> None
    def status(self) -> dict                       # GET /api/live/status body
async def run_ticks(hub: LiveHub, every: float = 5.0) -> None
async def run_bars(hub: LiveHub, stream=stream_bars) -> None
live_lifespan(app) -> AsyncContextManager       # run_ticks always; run_bars when Settings.lse_api_key is set
```

- [ ] **Step 1: Write the failing tests**

`tests/live/test_hub.py`:

```python
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from fenrir.live import arming, journal
from fenrir.live.service import LiveHub, run_bars
from fixtures import INSTRUMENTS, T0, bar


@pytest.fixture
def hub(settings):
    return LiveHub(settings, INSTRUMENTS)


def kinds(hub, account="Sim101"):
    return [r.kind for r in journal.rows(hub.conn, account=account)]


def test_runs_armed_dry_account(hub, armed):
    arming.insert_armed(hub.conn, armed)
    hub.tick(T0)
    hub.handle_bar(bar(T0))
    assert kinds(hub) == ["bar", "action"]
    assert hub.last_bar_ts == T0
    [acc] = hub.status()["accounts"]
    assert acc["account"] == "Sim101" and acc["mode"] == "dry"


def test_bar_picks_up_newly_armed_account_without_a_tick(hub, armed):
    arming.insert_armed(hub.conn, armed)
    hub.handle_bar(bar(T0))
    assert kinds(hub) == ["bar", "action"]


def test_ignores_bars_of_other_series(hub, armed):
    arming.insert_armed(hub.conn, armed)
    hub.handle_bar(bar(T0, contract="ESZ26"))
    assert kinds(hub) == []


def test_disarm_drops_account(hub, armed):
    arming.insert_armed(hub.conn, armed)
    hub.tick(T0)
    arming.delete_armed(hub.conn, "Sim101")
    hub.tick(T0 + timedelta(seconds=5))
    assert hub.states == {}


def test_broken_armed_row_alerts_once(hub, armed):
    arming.insert_armed(hub.conn, replace(armed, strategy_path="strategies/missing.py"))
    hub.tick(T0)
    hub.tick(T0 + timedelta(seconds=5))
    assert kinds(hub) == ["alert"]
    assert hub.status()["accounts"][0]["halted"] is True


def test_changed_file_is_journaled_as_halt(hub, armed):
    arming.insert_armed(hub.conn, replace(armed, strategy_hash="old"))
    hub.tick(T0)
    assert kinds(hub) == ["halt"]


def test_file_edited_while_armed_halts_on_next_bar(hub, armed):
    arming.insert_armed(hub.conn, armed)
    hub.handle_bar(bar(T0))
    with open(armed.strategy_path, "a") as f:
        f.write("\n# tweak\n")
    hub.handle_bar(bar(T0 + timedelta(minutes=1)))
    assert kinds(hub) == ["bar", "action", "halt", "bar", "rejection"]
    assert "changed since arming" in hub.states["Sim101"].halted


@pytest.mark.asyncio
async def test_run_bars_feeds_the_hub(hub, armed):
    arming.insert_armed(hub.conn, armed)

    async def fake_stream(settings, contracts):
        assert set(contracts) == {"ESZ26", "NQZ26"}
        yield bar(T0)
        await asyncio.sleep(3600)

    task = asyncio.create_task(run_bars(hub, fake_stream))
    for _ in range(100):
        if kinds(hub):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert kinds(hub) == ["bar", "action"]
    assert hub.lse_connected is True
```

`test_run_bars_feeds_the_hub` computes contracts from today's date; it relies on `INSTRUMENTS` having a roll on or before today for ES and NQ (2026-09-14). That holds until the next roll is added to the fixture.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_hub.py -q`
Expected: FAIL with `ImportError: cannot import name 'LiveHub' from 'fenrir.live.service'`

- [ ] **Step 3: Implement the hub** (PROVISIONAL (gate): `stream_bars` import and call)

Replace `src/fenrir/live/service.py` with:

```python
"""Live runner shell: loads armed accounts, feeds bars and ticks into the core, performs effects."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fenrir.core.bars import Bar
from fenrir.core.instruments import Instruments, front_month, load_instruments, spec
from fenrir.core.sessions import trading_day
from fenrir.data.live import stream_bars  # PROVISIONAL (gate)
from fenrir.live import core, journal
from fenrir.live.arming import Armed, list_armed
from fenrir.live.core import Effect, Log, RunnerState, Setup
from fenrir.rules.account import build_rules
from fenrir.rules.loader import load_account
from fenrir.settings import Settings
from fenrir.strategy.loader import file_hash, load_strategy


def load_setup(a: Armed, settings: Settings, instruments: Instruments) -> Setup:
    rules = load_account(a.rules_account, settings.rules_dir)
    return Setup(
        armed=a,
        strategy=load_strategy(Path(a.strategy_path)),
        rules=rules,
        rule_fns=build_rules(rules, a.phase),
        spec=spec(instruments, a.contract),
        safety_margin_pct=settings.safety_margin_pct,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_or_none(path: str) -> str | None:
    try:
        return file_hash(Path(path))
    except OSError:
        return None


class LiveHub:
    """Imperative shell around the pure core. The armed table is the source of truth for which accounts run."""

    def __init__(self, settings: Settings, instruments: Instruments | None = None) -> None:
        self.settings = settings
        self.instruments = instruments or load_instruments()
        self.conn = journal.open_db(settings.data_dir / "fenrir.db")
        self.states: dict[str, RunnerState] = {}
        self.broken: dict[str, tuple[Armed, str]] = {}  # rows that failed to load, so we alert once
        self.lse_connected = False
        self.last_bar_ts: datetime | None = None

    def sync(self, now: datetime) -> None:
        rows = {a.account: a for a in list_armed(self.conn)}
        for acc in [a for a, s in self.states.items() if rows.get(a) != s.setup.armed]:
            del self.states[acc]
        for acc in [a for a, (row, _) in self.broken.items() if rows.get(a) != row]:
            del self.broken[acc]
        for acc, a in rows.items():
            if acc in self.states or acc in self.broken:
                continue
            try:
                setup = load_setup(a, self.settings, self.instruments)
            except Exception as e:  # bad file, rules id or contract: report once, keep other accounts running
                self.broken[acc] = (a, repr(e))
                self.perform(acc, (Log("alert", {"message": f"cannot start: {e!r}"}),))
                continue
            self.states[acc], effects = core.start(setup, now)
            self.perform(acc, effects)

    def tick(self, now: datetime) -> None:
        self.sync(now)
        for acc, s in list(self.states.items()):
            self.states[acc], effects = core.on_tick(s, now, self.settings.stale_seconds)
            self.perform(acc, effects)

    def handle_bar(self, bar: Bar) -> None:
        self.sync(_now())
        self.last_bar_ts = bar.ts
        day = trading_day(bar.ts)
        for acc, s in list(self.states.items()):
            if bar.contract != front_month(self.instruments, s.setup.strategy.market, day):
                continue
            if _hash_or_none(s.setup.armed.strategy_path) != s.setup.armed.strategy_hash:
                s, effects = core.halt(s, "strategy file changed since arming; re-run the backtest and re-arm")
                self.perform(acc, effects)
            self.states[acc], effects = core.on_bar(s, bar)
            self.perform(acc, effects)

    def perform(self, account: str, effects: tuple[Effect, ...]) -> None:
        for e in effects:
            journal.write(self.conn, _now(), account, e.kind, e.payload)

    def status(self) -> dict:
        broken = [
            {"account": acc, "mode": a.mode, "strategy": Path(a.strategy_path).stem, "rules_account": a.rules_account,
             "contract": a.contract, "halted": True, "halt_reason": f"cannot start: {err}", "balance": None,
             "limits": None, "position": None}
            for acc, (a, err) in self.broken.items()
        ]
        return {
            "lse": {"connected": self.lse_connected, "last_bar_ts": self.last_bar_ts},
            "nt": {"connected": False, "last_heartbeat": None},
            "accounts": [core.account_status(s) for s in self.states.values()] + broken,
        }


async def run_ticks(hub: LiveHub, every: float = 5.0) -> None:
    while True:
        hub.tick(_now())
        await asyncio.sleep(every)


async def run_bars(hub: LiveHub, stream=stream_bars) -> None:
    while True:
        day = trading_day(_now())
        contracts = [front_month(hub.instruments, series, day) for series in ("ES", "NQ")]
        try:
            hub.lse_connected = True
            async for bar in stream(hub.settings, contracts):
                hub.handle_bar(bar)
                if trading_day(_now()) != day:
                    break  # resubscribe so a roll picks up the new front month
        except Exception as e:  # keep the runner alive; the stale-data pause covers the gap
            for acc in hub.states:
                hub.perform(acc, (Log("alert", {"message": f"LSE stream error: {e!r}"}),))
        hub.lse_connected = False
        await asyncio.sleep(5)


@asynccontextmanager
async def live_lifespan(app):
    hub: LiveHub = app.state.live
    tasks = [asyncio.create_task(run_ticks(hub))]
    if hub.settings.lse_api_key:
        tasks.append(asyncio.create_task(run_bars(hub)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
```

- [ ] **Step 4: Wire the hub into the server**

In `src/fenrir/server/app.py`, inside `create_app`:

1. Add at the top of the file:

```python
from fenrir.live.service import LiveHub, live_lifespan
```

2. Pass the lifespan where the app is constructed: `app = FastAPI(...)` becomes `app = FastAPI(..., lifespan=live_lifespan)`. If plan 11 already passes a `lifespan`, enter `live_lifespan(app)` inside it instead:

```python
    async with live_lifespan(app):
        yield
```

3. After the app is constructed (before `return app`):

```python
    app.state.live = LiveHub(settings)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/live -q && uv run pytest -q`
Expected: `34 passed` for `tests/live`; the whole suite passes (plan 11's server tests still pass with the hub attached).

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/live/service.py src/fenrir/server/app.py tests/live/test_hub.py
git commit -m "feat(live): run armed accounts on live bars from fenrir serve

LiveHub re-reads the armed table every tick and every bar, halts an
account whose strategy file changed since arming, routes front-month
bars and journals every effect. LSE streaming is provisional until the
data gate confirms the client API.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Live status and journal API

**Files:**
- Create: `src/fenrir/server/routes/live.py`
- Modify: `src/fenrir/server/app.py` (register router)
- Test: `tests/live/test_live_routes.py`

**Interfaces:**
- Consumes: `LiveHub` (Task 4), `journal.rows` (Task 1), `require_token`, `create_app` (11).
- Produces: `GET /api/live/status` → `{lse, nt, accounts: AccountStatus[]}`; `GET /api/live/journal?account=ID&day=YYYY-MM-DD` → `[{id, ts, account, kind, payload}]` (`day` defaults to the current trading day). Plan 22 adds `/kill` and `/resolve` to this router.

- [ ] **Step 1: Write the failing tests**

`tests/live/test_live_routes.py`:

```python
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from fenrir.live import arming, journal
from fenrir.live.service import LiveHub
from fenrir.server.app import create_app
from fixtures import INSTRUMENTS

TOKEN = "t0ken"
H = {"X-Fenrir-Token": TOKEN}


@pytest.fixture
def app(settings, armed):
    app = create_app(settings, TOKEN, None)
    app.state.live = LiveHub(settings, INSTRUMENTS)
    arming.insert_armed(app.state.live.conn, armed)
    return app


def test_status_requires_token(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/live/status").status_code == 401


def test_status_lists_armed_accounts(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        app.state.live.tick(datetime.now(UTC))
        body = c.get("/api/live/status", headers=H).json()
    assert body["lse"]["connected"] is False
    assert [a["account"] for a in body["accounts"]] == ["Sim101"]


def test_journal_by_account_and_day(app):
    conn = app.state.live.conn
    journal.write(conn, datetime(2026, 10, 12, 14, 0, tzinfo=UTC), "Sim101", "alert", {"message": "hi"})
    journal.write(conn, datetime(2026, 10, 13, 14, 0, tzinfo=UTC), "Sim101", "alert", {"message": "later"})
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        rows = c.get("/api/live/journal", params={"account": "Sim101", "day": "2026-10-12"}, headers=H).json()
    assert [(r["kind"], r["payload"]) for r in rows] == [("alert", {"message": "hi"})]
    assert set(rows[0]) == {"id", "ts", "account", "kind", "payload"}
```

If plan 11's missing-token response is not 401, use plan 11's status in `test_status_requires_token`; do not change plan 11.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/live/test_live_routes.py -q`
Expected: FAIL with `assert 404 == 401` (router not registered).

- [ ] **Step 3: Implement the router**

`src/fenrir/server/routes/live.py`:

```python
"""Live runner API. Thin: reads the hub and the journal."""

from dataclasses import asdict
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Request

from fenrir.core.sessions import trading_day
from fenrir.live import journal
from fenrir.server.auth import require_token

router = APIRouter(prefix="/api/live", dependencies=[Depends(require_token)])


@router.get("/status")
async def status(request: Request) -> dict:
    return request.app.state.live.status()


@router.get("/journal")
async def journal_rows(request: Request, account: str | None = None, day: date | None = None) -> list[dict]:
    rows = journal.rows(request.app.state.live.conn, account, day or trading_day(datetime.now(UTC)))
    return [asdict(r) for r in rows]
```

In `src/fenrir/server/app.py`, add `from fenrir.server.routes import live as live_routes` and, next to the other routers, `app.include_router(live_routes.router)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all tests pass, including `3 passed` in `tests/live/test_live_routes.py`.

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/server/routes/live.py src/fenrir/server/app.py tests/live/test_live_routes.py
git commit -m "feat(live): expose live status and journal over the api

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Dry run on live LSE data (manual)

**PROVISIONAL (gate):** needs a working `stream_bars` (plan 04) and an LSE key that passed gate item 3.

**Files:**
- Modify: `docs/data-gate.md` (append a dry-run section)

- [ ] **Step 1: Start the server with an LSE key, during US market hours**

Run: `FENRIR_LSE_API_KEY=<key> uv run fenrir serve`
Expected: server starts on `127.0.0.1:8000`.

- [ ] **Step 2: Arm a dry-run account on the local database** (plan 23 adds the UI; this dev-only step goes through `fenrir.live.arming`, the only writer of the table)

Run:

```bash
uv run python -c "
from datetime import UTC, datetime
from pathlib import Path
from fenrir.live import arming, journal
from fenrir.strategy.loader import file_hash
p = Path('strategies/orb.py')
arming.insert_armed(journal.open_db(Path('data/fenrir.db')), arming.Armed('DRY1', str(p), file_hash(p), 'lucid-flex-50k', 'MNQ', 'dry', 'evaluation', datetime.now(UTC)))
"
```

Expected: no output.

- [ ] **Step 3: Watch 15 minutes of bars**

Run (copy the token from the page's `<meta name="fenrir-token">`): `curl -s -H "X-Fenrir-Token: $TOKEN" "http://127.0.0.1:8000/api/live/journal?account=DRY1" | python -m json.tool | tail -40`
Expected: one `bar` row per minute with `contract` equal to today's NQ front month; `action` rows with `"sent": false` or `rejection` rows with a reason whenever the strategy emits a bracket; no `alert` rows while the market is open.

- [ ] **Step 4: Check status**

Run: `curl -s -H "X-Fenrir-Token: $TOKEN" http://127.0.0.1:8000/api/live/status | python -m json.tool`
Expected: `lse.connected: true`, `last_bar_ts` within the last 2 minutes, `DRY1` with `halted: false`.

- [ ] **Step 5: Check the stale-data pause**

Disconnect the network for 2 minutes, then reconnect.
Expected: one `alert` row "no NQ bar for …s; new entries paused"; status shows `halt_reason: "stale_data"`; the pause clears with the first bar after reconnecting.

- [ ] **Step 6: Disarm and record**

Run: `uv run python -c "from pathlib import Path; from fenrir.live import arming, journal; arming.delete_armed(journal.open_db(Path('data/fenrir.db')), 'DRY1')"`
Then append a dated "Dry run" section to `docs/data-gate.md`: bars per minute observed, any gaps, and whether actions matched what a backtest of the same minutes would emit.

- [ ] **Step 7: Commit**

```bash
git add docs/data-gate.md
git commit -m "docs(live): record first dry run on live lse data

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
