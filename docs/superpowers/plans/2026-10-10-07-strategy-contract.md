# Strategy Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define what a strategy file is (`MARKET`, `PARAMS`, `init`, `step`), the `Ctx`/`Position` values it receives, a loader that imports and validates a strategy file, and one working example strategy (`strategies/orb.py`).

**Architecture:** `fenrir.strategy.types` holds the frozen values a strategy sees (`Position`, `Ctx`). `fenrir.strategy.loader` is the only shell: it reads the file bytes once, rejects forbidden imports by AST scan, hashes the bytes, executes exactly those bytes as a uniquely named module and returns a frozen `Strategy`. Strategies are pure `init`/`step` functions; the same file is later run by the backtester (plan 08) and the live runner (plan 21).

**Tech Stack:** Python 3.12 stdlib (`ast`, `hashlib`, `types`, `dataclasses`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§5 Strategies)

**Index:** `docs/superpowers/plans/README.md` (cross-plan contracts: plan 01 core, plan 06 `AccountView`, plan 07 `fenrir.strategy`)

## Global Constraints

- Python 3.12, `uv`, package `fenrir` in `src/`. Run tests with `uv run pytest -q`; lint with `uv run ruff check . && uv run ruff format --check .`.
- Functional core, imperative shell: `@dataclass(frozen=True, slots=True)` values, tuples, pure functions; I/O only in `loader.py`.
- A strategy file exports `MARKET` (`"ES"` or `"NQ"`), `PARAMS` (dict), `init(params) -> State`, `step(state, bar, ctx) -> tuple[State, list[Action]]`.
- `Action = Bracket(side, qty, stop, target) | Flatten() | Cancel(order_id)`; there is no entry without a stop.
- `step` must be pure: no I/O, no clock, no randomness. The same file runs in backtests and live.
- Contract (verbatim from the index): `Position(qty, entry, stop, target)`, `Ctx(ts, bars, position, account, spec)`, `Strategy(name, path, hash, market, params, init, step)`, `load_strategy(path) -> Strategy`, `file_hash(path) -> str`.
- Every new directory gets a `CLAUDE.md` and a row in the root `CLAUDE.md` → **Directory Map**, in the same commit.
- Commits: Conventional Commits, scope `strategy`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A strategy file with a syntax error → `ValueError` naming the file, not a raw traceback from deep inside the loader. Test in Task 2.
2. Two strategy files with the same stem (`a/orb.py`, `b/orb.py`) loaded in one process → each keeps its own code; neither overwrites the other's module. Test in Task 2.
3. A strategy file edited between two loads → the second load has the new hash *and* runs the new code (the hash always describes the bytes that ran). Test in Task 2.
4. `from os import path` or `import urllib.request` (nested/aliased forbidden imports) → rejected, not only `import os`. Test in Task 2.
5. ORB on a day whose data starts after the opening range (first bar 10:00 ET) → no trade that day, no crash on `None` range. Test in Task 3.

---

### Task 1: Strategy values (`Position`, `Ctx`)

**Files:**
- Create: `src/fenrir/strategy/__init__.py`
- Create: `src/fenrir/strategy/types.py`
- Create: `src/fenrir/strategy/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/strategy/test_types.py`

**Interfaces:**
- Consumes: `fenrir.core.bars.Bar`, `fenrir.core.instruments.ContractSpec` (plan 01); `fenrir.rules.state.AccountView` (plan 06).
- Produces: `fenrir.strategy.types.Position`, `fenrir.strategy.types.Ctx`, and `FLAT: Position` (the flat position constant), all re-exported from `fenrir.strategy`.

- [ ] **Step 1: Write the failing test**

`tests/strategy/test_types.py`:

```python
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.rules.state import AccountView
from fenrir.strategy import FLAT, Ctx, Position


def test_flat_position_has_no_prices():
    assert FLAT == Position(qty=0, entry=None, stop=None, target=None)


def test_ctx_is_frozen():
    bar = Bar(datetime(2025, 10, 6, 13, 30, tzinfo=UTC), 1.0, 2.0, 0.5, 1.5, 10, "NQZ25")
    view = AccountView(
        phase="evaluation",
        drawdown_remaining=2000.0,
        daily_loss_remaining=None,
        today_pnl=0.0,
        target_remaining=3000.0,
    )
    ctx = Ctx(
        ts=bar.ts,
        bars=(bar,),
        position=FLAT,
        account=view,
        spec=ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0),
    )
    with pytest.raises(FrozenInstanceError):
        ctx.ts = bar.ts  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/strategy/test_types.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.strategy'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/strategy/types.py`:

```python
"""Values a strategy's `step` receives. Pure data."""

from dataclasses import dataclass
from datetime import datetime

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.rules.state import AccountView


@dataclass(frozen=True, slots=True)
class Position:
    qty: int  # signed contracts; 0 = flat
    entry: float | None
    stop: float | None
    target: float | None


FLAT = Position(qty=0, entry=None, stop=None, target=None)


@dataclass(frozen=True, slots=True)
class Ctx:
    ts: datetime  # current bar's open time
    bars: tuple[Bar, ...]  # last <= 500 bars, ending with the current bar
    position: Position
    account: AccountView
    spec: ContractSpec  # the contract being traded (tick, point_value)
```

`src/fenrir/strategy/__init__.py`:

```python
from fenrir.strategy.types import FLAT, Ctx, Position

__all__ = ["FLAT", "Ctx", "Position"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/strategy/test_types.py -q`
Expected: `2 passed`

- [ ] **Step 5: Add the directory context file and map row**

`src/fenrir/strategy/CLAUDE.md`:

```markdown
# src/fenrir/strategy/

The strategy contract: what a strategy file must export, the values its `step` receives, and the loader that imports a strategy file.

- Entry points: `types.py` (`Position`, `Ctx`, `FLAT`), `loader.py` (`Strategy`, `load_strategy`, `file_hash`).
- Pure vs shell: `types.py` is pure data; `loader.py` reads and executes files (shell).
- Not here: strategy files themselves (`strategies/`), running them over bars (`src/fenrir/backtest/`), running them live (`src/fenrir/live/`).
- Tests: `tests/strategy/`.
```

In root `CLAUDE.md`, add this row to the **Directory Map** table (after the existing rows):

```markdown
| `src/fenrir/strategy/` | Strategy contract (`Ctx`, `Position`, `Strategy`) and the loader that imports and validates strategy files. |
```

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/strategy/__init__.py src/fenrir/strategy/types.py src/fenrir/strategy/CLAUDE.md tests/strategy/test_types.py CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(strategy): add Position and Ctx strategy values

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Strategy loader

**Files:**
- Create: `src/fenrir/strategy/loader.py`
- Modify: `src/fenrir/strategy/__init__.py`
- Test: `tests/strategy/test_loader.py`

**Interfaces:**
- Consumes: `fenrir.core.actions.Action` (plan 01), `fenrir.strategy.types.Ctx` (Task 1).
- Produces: `fenrir.strategy.loader.Strategy` (frozen: `name: str`, `path: Path`, `hash: str`, `market: str`, `params: Mapping[str, Any]`, `init`, `step`), `load_strategy(path: Path) -> Strategy` (raises `ValueError` with the path in the message), `file_hash(path: Path) -> str` (sha256 hex of the file bytes), `MARKETS = ("ES", "NQ")`, `FORBIDDEN_IMPORTS: frozenset[str]`. Re-exported from `fenrir.strategy`.

- [ ] **Step 1: Write the failing tests**

`tests/strategy/test_loader.py`:

```python
import hashlib
from pathlib import Path

import pytest

from fenrir.strategy import file_hash, load_strategy

GOOD = '''
MARKET = "NQ"
PARAMS = {"n": 3}

def init(params):
    return params["n"]

def step(state, bar, ctx):
    return state, []
'''


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_loads_valid_strategy(tmp_path):
    path = write(tmp_path / "s.py", GOOD)
    s = load_strategy(path)
    assert s.name == "s"
    assert s.path == path
    assert s.market == "NQ"
    assert s.params == {"n": 3}
    assert s.init(s.params) == 3
    assert s.step(3, None, None) == (3, [])
    assert s.hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_hash(path) == s.hash


@pytest.mark.parametrize("missing", ["MARKET", "PARAMS", "init", "step"])
def test_rejects_missing_export(tmp_path, missing):
    text = GOOD.replace(f"{missing} =", f"_{missing} =").replace(f"def {missing}(", f"def _{missing}(")
    path = write(tmp_path / "s.py", text)
    with pytest.raises(ValueError, match=f"missing {missing}"):
        load_strategy(path)


def test_rejects_unknown_market(tmp_path):
    path = write(tmp_path / "s.py", GOOD.replace('"NQ"', '"CL"'))
    with pytest.raises(ValueError, match="MARKET must be one of ES, NQ"):
        load_strategy(path)


def test_rejects_non_dict_params(tmp_path):
    path = write(tmp_path / "s.py", GOOD.replace('{"n": 3}', "[3]"))
    with pytest.raises(ValueError, match="PARAMS must be a dict"):
        load_strategy(path)


@pytest.mark.parametrize(
    "line",
    ["import random", "import os", "from os import path", "import urllib.request", "import time as t"],
)
def test_rejects_forbidden_imports(tmp_path, line):
    path = write(tmp_path / "s.py", line + "\n" + GOOD)
    with pytest.raises(ValueError, match="forbidden import"):
        load_strategy(path)


def test_syntax_error_names_the_file(tmp_path):
    path = write(tmp_path / "broken.py", "def step(:\n")
    with pytest.raises(ValueError, match="broken.py"):
        load_strategy(path)


def test_same_stem_in_two_dirs_keeps_both(tmp_path):
    a = load_strategy(write(tmp_path / "a" / "orb.py", GOOD))
    b = load_strategy(write(tmp_path / "b" / "orb.py", GOOD.replace('{"n": 3}', '{"n": 7}')))
    assert a.init(a.params) == 3
    assert b.init(b.params) == 7


def test_edited_file_reloads_new_code_and_hash(tmp_path):
    path = write(tmp_path / "s.py", GOOD)
    first = load_strategy(path)
    write(path, GOOD.replace("return params[\"n\"]", "return params[\"n\"] * 10"))
    second = load_strategy(path)
    assert second.hash != first.hash
    assert second.init(second.params) == 30
    assert first.init(first.params) == 3


def test_dataclass_state_works(tmp_path):
    text = "from dataclasses import dataclass\n\n@dataclass(frozen=True, slots=True)\nclass S:\n    n: int\n" + GOOD.replace(
        'return params["n"]', 'return S(params["n"])'
    )
    s = load_strategy(write(tmp_path / "d.py", text))
    assert s.init(s.params).n == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/strategy/test_loader.py -q`
Expected: FAIL with `ImportError: cannot import name 'file_hash' from 'fenrir.strategy'`

- [ ] **Step 3: Write minimal implementation**

`src/fenrir/strategy/loader.py`:

```python
"""Load a strategy file: scan imports, hash the bytes, execute exactly those bytes. Shell."""

import ast
import hashlib
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fenrir.core.actions import Action
from fenrir.core.bars import Bar
from fenrir.strategy.types import Ctx

MARKETS = ("ES", "NQ")
# Modules that give a strategy I/O, a clock or randomness. A static check, not a sandbox.
FORBIDDEN_IMPORTS = frozenset(
    {"asyncio", "io", "os", "pathlib", "random", "requests", "httpx", "secrets", "shutil",
     "socket", "subprocess", "sys", "threading", "time", "urllib", "uuid"}
)


@dataclass(frozen=True, slots=True)
class Strategy:
    name: str  # file stem
    path: Path
    hash: str  # sha256 hex of file bytes
    market: str  # "ES" | "NQ"
    params: Mapping[str, Any]
    init: Callable[[Mapping[str, Any]], Any]
    step: Callable[[Any, Bar, Ctx], tuple[Any, list[Action]]]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _imported_roots(tree: ast.Module) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def load_strategy(path: Path) -> Strategy:
    source = path.read_bytes()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise ValueError(f"{path}: syntax error: {e}") from e
    forbidden = sorted(_imported_roots(tree) & FORBIDDEN_IMPORTS)
    if forbidden:
        raise ValueError(f"{path}: forbidden import {', '.join(forbidden)} (step must be pure)")

    digest = hashlib.sha256(source).hexdigest()
    name = f"fenrir_strategy_{path.stem}_{digest[:12]}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module  # dataclasses defined in the file resolve their module here
    exec(compile(tree, str(path), "exec"), module.__dict__)

    missing = [n for n in ("MARKET", "PARAMS", "init", "step") if not hasattr(module, n)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    if module.MARKET not in MARKETS:
        raise ValueError(f"{path}: MARKET must be one of ES, NQ, got {module.MARKET!r}")
    if not isinstance(module.PARAMS, dict):
        raise ValueError(f"{path}: PARAMS must be a dict")
    if not (callable(module.init) and callable(module.step)):
        raise ValueError(f"{path}: init and step must be functions")
    return Strategy(
        name=path.stem,
        path=path,
        hash=digest,
        market=module.MARKET,
        params=dict(module.PARAMS),
        init=module.init,
        step=module.step,
    )
```

`src/fenrir/strategy/__init__.py` (replace):

```python
from fenrir.strategy.loader import FORBIDDEN_IMPORTS, MARKETS, Strategy, file_hash, load_strategy
from fenrir.strategy.types import FLAT, Ctx, Position

__all__ = [
    "FLAT",
    "FORBIDDEN_IMPORTS",
    "MARKETS",
    "Ctx",
    "Position",
    "Strategy",
    "file_hash",
    "load_strategy",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/strategy -q`
Expected: `18 passed`

- [ ] **Step 5: Commit**

```bash
git add src/fenrir/strategy/loader.py src/fenrir/strategy/__init__.py tests/strategy/test_loader.py
git commit -m "$(cat <<'EOF'
feat(strategy): load and validate strategy files

Scans imports, hashes the file bytes and executes exactly those bytes,
so the hash always describes the code that ran.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Example opening-range breakout strategy

**Files:**
- Create: `strategies/orb.py`
- Create: `strategies/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/strategy/test_orb.py`

**Interfaces:**
- Consumes: `load_strategy` (Task 2), `Ctx`, `FLAT`, `Position` (Task 1), `fenrir.core.actions.Bracket`, `fenrir.core.sessions.NY`, `fenrir.core.sessions.trading_day` (plan 01).
- Produces: `strategies/orb.py` with `MARKET = "NQ"`, `PARAMS = {"range_min": 15, "stop_ticks": 40, "target_ticks": 80, "qty": 1}`. Plans 08–10 use it as the reference strategy.

- [ ] **Step 1: Write the failing test**

`tests/strategy/test_orb.py`:

```python
from datetime import UTC, date, datetime
from pathlib import Path

from fenrir.core.actions import Bracket
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import NY
from fenrir.rules.state import AccountView
from fenrir.strategy import FLAT, Ctx, Position, load_strategy

ORB = Path(__file__).parents[2] / "strategies" / "orb.py"
NQ = ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0)
VIEW = AccountView("evaluation", 2000.0, None, 0.0, 3000.0)
DAY = date(2025, 10, 6)


def bar(hhmm: str, high: float, low: float, close: float, day: date = DAY) -> Bar:
    h, m = map(int, hhmm.split(":"))
    ts = datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(UTC)
    return Bar(ts, close, high, low, close, 100, "NQZ25")


def run(bars: list[Bar], position: Position = FLAT) -> list[list]:
    s = load_strategy(ORB)
    state = s.init(s.params)
    out = []
    for b in bars:
        state, actions = s.step(state, b, Ctx(b.ts, (b,), position, VIEW, NQ))
        out.append(actions)
    return out


def opening_range(day: date = DAY) -> list[Bar]:
    # 09:30–09:44 ET: range high 102, low 98
    return [bar(f"09:{30 + i}", 102.0 if i == 3 else 101.0, 98.0 if i == 7 else 99.0, 100.0, day) for i in range(15)]


def test_loads():
    s = load_strategy(ORB)
    assert s.market == "NQ"
    assert s.params == {"range_min": 15, "stop_ticks": 40, "target_ticks": 80, "qty": 1}


def test_no_action_inside_the_range():
    assert all(a == [] for a in run(opening_range()))


def test_long_breakout_brackets_from_close():
    actions = run([*opening_range(), bar("09:45", 103.0, 101.0, 102.5)])
    assert actions[-1] == [Bracket("long", 1, 102.5 - 40 * 0.25, 102.5 + 80 * 0.25)]


def test_short_breakout():
    actions = run([*opening_range(), bar("09:45", 99.0, 97.0, 97.5)])
    assert actions[-1] == [Bracket("short", 1, 97.5 + 10.0, 97.5 - 20.0)]


def test_one_trade_per_day_and_resets_next_day():
    day2 = date(2025, 10, 7)
    bars = [
        *opening_range(),
        bar("09:45", 103.0, 101.0, 102.5),
        bar("09:46", 104.0, 102.0, 103.5),
        *opening_range(day2),
        bar("09:45", 103.0, 101.0, 102.5, day2),
    ]
    actions = run(bars)
    assert actions[15] != [] and actions[16] == []
    assert actions[-1] == [Bracket("long", 1, 92.5, 122.5)]


def test_no_entry_while_in_position():
    actions = run([*opening_range(), bar("09:45", 103.0, 101.0, 102.5)], Position(1, 100.0, 90.0, 120.0))
    assert actions[-1] == []


def test_no_trade_when_data_starts_after_the_range():
    assert run([bar("10:00", 120.0, 80.0, 119.0), bar("10:01", 130.0, 70.0, 75.0)]) == [[], []]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/strategy/test_orb.py -q`
Expected: FAIL with `FileNotFoundError` for `strategies/orb.py`

- [ ] **Step 3: Write the strategy**

`strategies/orb.py`:

```python
"""Opening-range breakout. The first `range_min` minutes after 09:30 ET set a range; the
first bar that closes outside it enters one bracket in that direction. One trade per day."""

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from fenrir.core.actions import Bracket
from fenrir.core.sessions import NY, trading_day

MARKET = "NQ"
PARAMS = {"range_min": 15, "stop_ticks": 40, "target_ticks": 80, "qty": 1}

OPEN_MINUTE = 9 * 60 + 30


@dataclass(frozen=True, slots=True)
class State:
    params: dict[str, Any]
    day: date | None = None
    high: float | None = None
    low: float | None = None
    traded: bool = False


def init(params):
    return State(params=dict(params))


def step(state, bar, ctx):
    day = trading_day(bar.ts)
    if day != state.day:
        state = State(params=state.params, day=day)
    local = bar.ts.astimezone(NY)
    minute = local.hour * 60 + local.minute - OPEN_MINUTE
    p = state.params
    if minute < 0:
        return state, []
    if minute < p["range_min"]:
        high = bar.high if state.high is None else max(state.high, bar.high)
        low = bar.low if state.low is None else min(state.low, bar.low)
        return replace(state, high=high, low=low), []
    if state.traded or state.high is None or ctx.position.qty != 0:
        return state, []
    if bar.close > state.high:
        side, sign = "long", 1
    elif bar.close < state.low:
        side, sign = "short", -1
    else:
        return state, []
    tick = ctx.spec.tick
    stop = bar.close - sign * p["stop_ticks"] * tick
    target = bar.close + sign * p["target_ticks"] * tick
    return replace(state, traded=True), [Bracket(side, p["qty"], stop, target)]
```

Note: the range is only built from bars inside the window, so a day whose first bar is after 09:45 ET keeps `high = None` and never trades (Review Focus 5).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/strategy -q`
Expected: `25 passed`

- [ ] **Step 5: Add the directory context file and map row**

`strategies/CLAUDE.md`:

```markdown
# strategies/

Strategy files written by Claude. One file = one strategy; the file stem is its name.

- Contract: export `MARKET` ("ES" or "NQ"; the contract, e.g. MNQ, is chosen per run), `PARAMS` (dict), `init(params) -> state`, `step(state, bar, ctx) -> (state, [actions])`.
- Actions: `Bracket(side, qty, stop, target)` with absolute prices, `Flatten()`, `Cancel(order_id)` from `fenrir.core.actions`. No entry without a stop.
- `step` must be pure: no I/O, no clock (`datetime.now()`), no randomness. Use `bar.ts`/`ctx.ts` for time. Keep state in frozen dataclasses; put params in the state (step does not receive them).
- `ctx`: `ts`, `bars` (last <= 500, ending with the current bar), `position`, `account` (drawdown/daily-loss/target remaining, phase), `spec` (tick, point_value). See `src/fenrir/strategy/types.py`.
- Imports of os, time, random, socket, etc. are rejected by the loader. Editing a file changes its hash; arming then needs a fresh backtest.
- Example: `orb.py`. Tests: `tests/strategy/`.
```

In root `CLAUDE.md`, add this row to the **Directory Map** table:

```markdown
| `strategies/` | Strategy files written by Claude; each exports `MARKET`, `PARAMS`, `init`, `step`. Contract in `strategies/CLAUDE.md`. |
```

- [ ] **Step 6: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` and no files to reformat (run `uv run ruff format .` if needed and re-run tests).

- [ ] **Step 7: Commit**

```bash
git add strategies/orb.py strategies/CLAUDE.md tests/strategy/test_orb.py CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(strategy): add opening-range breakout example strategy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```
