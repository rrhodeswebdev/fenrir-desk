# History Pull Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `fenrir data pull` downloads 1-minute ES and NQ bars from LSE, one Parquet file per contract, and `fenrir data summary` reports coverage and gaps; `load_bars` gives the backtester unadjusted front-month bars, and `data_version` identifies the data for result caching.

**Architecture:** `fenrir.data.pull` turns the rollover calendar into per-contract download windows and decides which still need pulling (pure). `fenrir.data.store` reads and writes `data/bars/{ES,NQ}/1m/<contract>.parquet` and `data/bars/manifest.json`, and selects front-month rows per trading day by joining against `front_month` (polars). `fenrir.data.lse` is the **only** LSE-aware module; it is injected into the pull as a plain `fetch` function, so tests never touch the network.

**Tech Stack:** Python 3.12, `polars`, `pyarrow`, `pandas-market-calendars` (CME trading days for gap detection), `lse-data`, `typer`.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§2 Storage, §3 History, Contracts and rollover)
**Index:** `docs/superpowers/plans/README.md` ("Plan 03 — `fenrir.data.store`")

> **PROVISIONAL (gate):** `src/fenrir/data/lse.py` and `DEFAULT_SINCE` are written against the expected `lse-data` API. Task 1 reconciles them with `docs/data-gate.md` before anything else.

## Global Constraints

- Requires plan 02's Decision to be GO for items 1–2. If it says STOP, do not execute this plan.
- 1-minute OHLCV bars for ES and NQ only; MES and MNQ use the ES and NQ prices.
- Prices are **unadjusted front-month**, never back-adjusted continuous. Front month comes from `fenrir.core.instruments.front_month`, the same calendar live trading uses.
- Storage: `data/bars/{ES,NQ}/1m/<contract>.parquet` (columns `ts, open, high, low, close, volume`; `ts` = bar open, UTC) and `data/bars/manifest.json`. `data/` is gitignored except `data/CLAUDE.md`.
- `fenrir.data.lse` is the only code that knows about LSE; swapping providers means rewriting that file alone.
- `data_version` changes on every pull (sha256 of the manifest bytes).
- Functional core, imperative shell: pure planning and selection; file and network I/O in thin functions.
- Directory context file rule: `src/fenrir/data/` and `data/` get a `CLAUDE.md` and a Directory Map row in the commit that creates them.
- Commits: Conventional Commits, scope `data`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Interrupted pull** (network drop, Ctrl-C): contracts finished before the failure stay recorded and the manifest stays valid JSON; the next pull resumes. Pinned in Task 4 (`test_pull_failure_keeps_finished_contracts`).
2. **Roll day**: on the roll Thursday the backtester must get the new contract's prices, never both contracts and never the old one. Pinned in Task 3 (`test_load_bars_uses_front_month_per_trading_day`).
3. **Evening session**: a bar at 18:30 New York belongs to the next trading day, so `start`/`end` filtering and front-month choice use the trading day, not the UTC date. Pinned in Task 3 (`test_load_bars_filters_by_trading_day`).
4. **Messy provider output**: duplicated or unsorted rows from LSE must be stored once, in time order. Pinned in Task 3 (`test_write_contract_dedupes_and_sorts`).
5. **No data yet**: a fresh clone must get `()` from `load_bars` and a clear "run fenrir data pull" from `summary`, not a crash. Pinned in Task 3 (`test_load_bars_empty_dir`) and Task 4 (`test_summary_without_data`).

---

### Task 1: Reconcile with the data gate

**Files:**
- Modify: this plan (Task 4 code blocks for `src/fenrir/data/lse.py` and `DEFAULT_SINCE`)

**Interfaces:**
- Consumes: `docs/data-gate.md` (plan 02).
- Produces: corrected provisional code in this plan.

- [ ] **Step 1: Read the gate results**

Run: `sed -n '/## Client API/,/## Item 1/p;/## Values downstream/,$p' docs/data-gate.md`
Expected: a filled Client API table, filled "Values downstream plans need", and a Decision that says GO for plan 03. If the Decision is STOP or missing, stop and tell the user.

- [ ] **Step 2: Update the provisional code in Task 4**

In Task 4 Step 3 of this plan, edit before executing:
- `lse_symbol`: map our code (`NQZ26`) to the LSE symbol format from "LSE symbol for our code `NQZ26`". If LSE has no per-contract symbols and the continuous series is unadjusted front-month, have `fetch_contract` fetch the continuous symbol for the window's dates and set `lse_symbol` to return the series root.
- `fetch_contract`: client construction, call name, argument names, and row field names from the Client API table.
- Timestamp shift: if the gate recorded "bar close", subtract one minute in `fetch_contract` so `ts` is bar open. If exchange-local, convert to UTC.
- `DEFAULT_SINCE` in `src/fenrir/cli/data.py`: the "earliest usable day".
- If that day is before 2008, lower `rollover.first_year` in `instruments.yaml`.

- [ ] **Step 3: Commit the plan edit**

```bash
git add docs/superpowers/plans/2026-10-10-03-history-pull.md instruments.yaml
git commit -m "$(cat <<'EOF'
docs(data): align history pull plan with data gate findings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Pull planning (which contract windows to download)

**Files:**
- Create: `src/fenrir/data/__init__.py` (empty)
- Create: `src/fenrir/data/pull.py`
- Create: `src/fenrir/data/CLAUDE.md`
- Test: `tests/data/test_pull.py`
- Modify: `pyproject.toml`, `uv.lock` (deps), `CLAUDE.md` (Directory Map)

**Interfaces:**
- Consumes: `Instruments`, `Roll`, `parse_instruments` from `fenrir.core.instruments` (plan 01).
- Produces: `Window(series: str, contract: str, first: date, last: date)` (trading days, inclusive); `pull_windows(instruments, series, since, today) -> tuple[Window, ...]`; `todo(windows, manifest: Mapping, today: date) -> tuple[Window, ...]`.

- [ ] **Step 1: Add dependencies**

```bash
uv add polars pyarrow pandas-market-calendars
uv run python -c "import pandas_market_calendars as m; print([d.date().isoformat() for d in m.get_calendar('CME_Equity').valid_days('2025-11-24', '2025-12-01')])"
```

Expected: `['2025-11-24', '2025-11-25', '2025-11-26', '2025-11-28', '2025-12-01']` (Thanksgiving 2025-11-27 absent). If the calendar name errors, list names with `m.get_calendar_names()` and use the CME equity one in Task 3.

- [ ] **Step 2: Write the failing tests**

`tests/data/test_pull.py`:

```python
from datetime import date

from fenrir.core.instruments import parse_instruments
from fenrir.data.pull import Window, pull_windows, todo

INST = parse_instruments(
    {
        "contracts": [{"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1}],
        "rollover": {"series": ["NQ"], "first_year": 2025, "last_year": 2026},
    }
)


def test_pull_windows_cover_since_to_today_by_contract():
    assert pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20)) == (
        Window("NQ", "NQZ25", date(2025, 12, 1), date(2025, 12, 10)),
        Window("NQ", "NQH26", date(2025, 12, 11), date(2026, 3, 11)),
        Window("NQ", "NQM26", date(2026, 3, 12), date(2026, 3, 20)),
    )


def test_todo_skips_complete_contracts_only():
    windows = pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20))
    manifest = {
        "series": {
            "NQ": {
                "NQZ25": {"first_day": "2025-12-01", "last_day": "2025-12-10", "rows": 9, "complete": True},
                "NQH26": {"first_day": "2025-12-11", "last_day": "2026-02-01", "rows": 9, "complete": False},
            }
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2026, 3, 20))] == ["NQH26", "NQM26"]


def test_todo_repulls_when_since_moves_earlier():
    windows = pull_windows(INST, "NQ", date(2025, 11, 1), date(2025, 12, 5))
    manifest = {
        "series": {
            "NQ": {"NQZ25": {"first_day": "2025-12-01", "last_day": "2025-12-10", "rows": 9, "complete": True}}
        }
    }
    assert [w.contract for w in todo(windows, manifest, date(2025, 12, 5))] == ["NQZ25"]


def test_todo_empty_manifest_pulls_everything():
    windows = pull_windows(INST, "NQ", date(2025, 12, 1), date(2026, 3, 20))
    assert todo(windows, {}, date(2026, 3, 20)) == windows
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `mkdir -p src/fenrir/data && touch src/fenrir/data/__init__.py && uv run pytest tests/data/test_pull.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.data.pull'`.

- [ ] **Step 4: Write the implementation**

`src/fenrir/data/pull.py`:

```python
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fenrir.core.instruments import Instruments


@dataclass(frozen=True, slots=True)
class Window:
    series: str
    contract: str
    first: date  # first trading day pulled, inclusive
    last: date  # last trading day pulled, inclusive


def pull_windows(instruments: Instruments, series: str, since: date, today: date) -> tuple[Window, ...]:
    rolls = [r for r in instruments.rolls if r.series == series]
    ends = [r.start - timedelta(days=1) for r in rolls[1:]] + [today]
    windows = (
        Window(series, r.contract, max(r.start, since), min(end, today))
        for r, end in zip(rolls, ends, strict=True)
    )
    return tuple(w for w in windows if w.first <= w.last)


def todo(windows: tuple[Window, ...], manifest: Mapping[str, Any], today: date) -> tuple[Window, ...]:
    def done(w: Window) -> bool:
        entry = manifest.get("series", {}).get(w.series, {}).get(w.contract)
        return bool(entry) and entry["complete"] and date.fromisoformat(entry["first_day"]) <= w.first

    return tuple(w for w in windows if not done(w))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/data/test_pull.py`
Expected: `4 passed`.

- [ ] **Step 6: Add `src/fenrir/data/CLAUDE.md` and the Directory Map row**

`src/fenrir/data/CLAUDE.md`:

```markdown
# src/fenrir/data/

Market data: pulling LSE history into Parquet, loading front-month bars, and (plan 04) building live 1-minute bars.

- Entry points: `store.py` (`load_bars`, `data_version`, `coverage`, `write_bars` for fixtures), `pull.py` (`pull_windows`, `todo`, `pull`), `lse.py` (LSE fetch).
- Pure vs shell: `pull_windows`, `todo`, `find_gaps`, `with_contract` are pure; `store.py` file I/O and `pull()` are shell.
- `lse.py` is the ONLY module that knows about London Strategic Edge. Swapping providers = rewriting it alone.
- Prices are unadjusted front-month; front month comes from `fenrir.core.instruments.front_month`.
- Not here: the files themselves (`data/bars/`), CLI (`cli/data.py`).
- Tests: `tests/data/`.
```

Append to the end of the table in root `CLAUDE.md` → "## 6. Directory Map":

```markdown
| `src/fenrir/data/` | LSE history pull, Parquet store, front-month bar loading, live bar building. Only `lse.py` knows LSE. |
```

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format . && uv run ruff check .
git add pyproject.toml uv.lock src/fenrir/data tests/data CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(data): plan per-contract history pull windows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Parquet store, front-month loading, coverage

**Files:**
- Create: `src/fenrir/data/store.py`
- Create: `data/CLAUDE.md`
- Test: `tests/data/test_store.py`
- Modify: `.gitignore`, `CLAUDE.md` (Directory Map)

**Interfaces:**
- Consumes: `Bar` (`fenrir.core.bars`), `Instruments`, `front_month` (`fenrir.core.instruments`), `trading_day` (test only), `Window` (Task 2).
- Produces (exact, from index): `load_bars(data_dir, instruments, series, start=None, end=None) -> tuple[Bar, ...]`; `data_version(data_dir) -> str`; `coverage(data_dir, series) -> Coverage`; `Coverage(series, first_day, last_day, days, gaps)`.
- Also produces `write_bars(data_dir: Path, series: str, contract: str, bars: Sequence[Bar]) -> None`: merges bars by `ts` into `data/bars/<series>/1m/<contract>.parquet` (new rows win) and refreshes `manifest.json` (so `data_version` changes). Plan 14's e2e fixture generator uses it.
- Also produces (used by Task 4 and plan 16): `SCHEMA`, `read_manifest(data_dir) -> dict`, `write_manifest(data_dir, manifest) -> None`, `with_contract(manifest, window, rows, today, pulled_at) -> dict` (pure), `write_contract(data_dir, series, contract, frame) -> int` (rows written), `find_gaps(present, expected) -> tuple[tuple[date, date], ...]` (pure).

- [ ] **Step 1: Write the failing tests**

`tests/data/test_store.py`:

```python
import hashlib
import json
from datetime import UTC, date, datetime

import polars as pl

from fenrir.core.bars import Bar
from fenrir.core.instruments import parse_instruments
from fenrir.core.sessions import trading_day
from fenrir.data.pull import Window
from fenrir.data.store import (
    coverage,
    data_version,
    find_gaps,
    load_bars,
    read_manifest,
    trading_day_expr,
    with_contract,
    write_bars,
    write_contract,
    write_manifest,
)

INST = parse_instruments(
    {
        "contracts": [{"symbol": "NQ", "series": "NQ", "point_value": 20, "tick": 0.25, "minis": 1}],
        "rollover": {"series": ["NQ"], "first_year": 2025, "last_year": 2026},
    }
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def frame(rows: list[tuple[datetime, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [ts for ts, _ in rows],
            "open": [p for _, p in rows],
            "high": [p for _, p in rows],
            "low": [p for _, p in rows],
            "close": [p for _, p in rows],
            "volume": [1 for _ in rows],
        }
    )


def roll_fixture(data_dir):
    # NQ rolls Z25 -> H26 on trading day 2025-12-11. 23:30 UTC on 12-10 is 18:30 EST = trading day 12-11.
    times = [utc(2025, 12, 10, 14, 30), utc(2025, 12, 10, 23, 30), utc(2025, 12, 11, 14, 30)]
    write_contract(data_dir, "NQ", "NQZ25", frame(list(zip(times, [1.0, 2.0, 3.0], strict=True))))
    write_contract(data_dir, "NQ", "NQH26", frame(list(zip(times, [11.0, 12.0, 13.0], strict=True))))


def bar(ts: datetime, p: float, contract: str) -> Bar:
    return Bar(ts, p, p, p, p, 1, contract)


def test_load_bars_uses_front_month_per_trading_day(tmp_path):
    roll_fixture(tmp_path)
    assert load_bars(tmp_path, INST, "NQ") == (
        bar(utc(2025, 12, 10, 14, 30), 1.0, "NQZ25"),
        bar(utc(2025, 12, 10, 23, 30), 12.0, "NQH26"),
        bar(utc(2025, 12, 11, 14, 30), 13.0, "NQH26"),
    )


def test_load_bars_filters_by_trading_day(tmp_path):
    roll_fixture(tmp_path)
    day = date(2025, 12, 11)
    assert [b.close for b in load_bars(tmp_path, INST, "NQ", day, day)] == [12.0, 13.0]
    day = date(2025, 12, 10)
    assert [b.close for b in load_bars(tmp_path, INST, "NQ", day, day)] == [1.0]


def test_load_bars_empty_dir(tmp_path):
    assert load_bars(tmp_path, INST, "NQ") == ()


def test_write_contract_dedupes_and_sorts(tmp_path):
    rows = [(utc(2025, 12, 1, 14, 31), 2.0), (utc(2025, 12, 1, 14, 30), 1.0), (utc(2025, 12, 1, 14, 31), 9.0)]
    assert write_contract(tmp_path, "NQ", "NQZ25", frame(rows)) == 2
    assert [b.close for b in load_bars(tmp_path, INST, "NQ")] == [1.0, 9.0]


def test_trading_day_expr_matches_core():
    times = [
        utc(2026, 3, 8, 22, 30),
        utc(2026, 11, 1, 22, 30),
        utc(2026, 11, 1, 23, 0),
        utc(2026, 10, 8, 21, 59),
        utc(2026, 10, 8, 22, 0),
    ]
    got = pl.DataFrame({"ts": times}).select(trading_day_expr()).to_series().to_list()
    assert got == [trading_day(t) for t in times]


def test_find_gaps():
    expected = [date(2025, 11, d) for d in (24, 25, 26, 28)] + [date(2025, 12, 1), date(2025, 12, 2)]
    present = {date(2025, 11, 24), date(2025, 11, 28), date(2025, 12, 2)}
    assert find_gaps(present, expected) == (
        (date(2025, 11, 25), date(2025, 11, 26)),
        (date(2025, 12, 1), date(2025, 12, 1)),
    )


def test_coverage_skips_cme_holidays(tmp_path):
    days = [(2025, 11, 24), (2025, 11, 25), (2025, 11, 28), (2025, 12, 1)]
    write_contract(tmp_path, "NQ", "NQZ25", frame([(utc(*d, 15, 0), 1.0) for d in days]))
    assert coverage(tmp_path, "NQ").gaps == ((date(2025, 11, 26), date(2025, 11, 26)),)
    c = coverage(tmp_path, "NQ")
    assert (c.first_day, c.last_day, c.days) == (date(2025, 11, 24), date(2025, 12, 1), 4)


def test_coverage_empty(tmp_path):
    c = coverage(tmp_path, "ES")
    assert (c.first_day, c.last_day, c.days, c.gaps) == (None, None, 0, ())


def test_manifest_and_data_version(tmp_path):
    assert data_version(tmp_path) == hashlib.sha256(b"").hexdigest()
    assert read_manifest(tmp_path) == {"series": {}}
    w = Window("NQ", "NQZ25", date(2025, 9, 11), date(2025, 12, 10))
    m = with_contract({"series": {}}, w, rows=5, today=date(2025, 12, 20), pulled_at="2025-12-20T00:00:00Z")
    write_manifest(tmp_path, m)
    assert read_manifest(tmp_path) == m
    assert m["series"]["NQ"]["NQZ25"] == {
        "first_day": "2025-09-11",
        "last_day": "2025-12-10",
        "rows": 5,
        "complete": True,
    }
    first = data_version(tmp_path)
    write_manifest(tmp_path, {**m, "pulled_at": "2025-12-21T00:00:00Z"})
    assert data_version(tmp_path) != first
    assert json.loads((tmp_path / "bars" / "manifest.json").read_text())["pulled_at"] == "2025-12-21T00:00:00Z"


def test_write_bars_merges_and_refreshes_manifest(tmp_path):
    a = bar(utc(2025, 12, 1, 14, 30), 1.0, "NQZ25")
    b = bar(utc(2025, 12, 1, 14, 31), 2.0, "NQZ25")
    write_bars(tmp_path, "NQ", "NQZ25", [a, b])
    first = data_version(tmp_path)
    b2 = bar(utc(2025, 12, 1, 14, 31), 5.0, "NQZ25")
    c = bar(utc(2025, 12, 2, 14, 30), 3.0, "NQZ25")
    write_bars(tmp_path, "NQ", "NQZ25", [b2, c])
    assert load_bars(tmp_path, INST, "NQ") == (a, b2, c)
    entry = read_manifest(tmp_path)["series"]["NQ"]["NQZ25"]
    assert (entry["first_day"], entry["last_day"], entry["rows"]) == ("2025-12-01", "2025-12-02", 3)
    assert data_version(tmp_path) != first


def test_with_contract_current_window_is_incomplete():
    w = Window("NQ", "NQH26", date(2025, 12, 11), date(2026, 1, 5))
    m = with_contract({"series": {}}, w, rows=1, today=date(2026, 1, 5), pulled_at="x")
    assert m["series"]["NQ"]["NQH26"]["complete"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/data/test_store.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'fenrir.data.store'`.

- [ ] **Step 3: Write the implementation**

`src/fenrir/data/store.py`:

```python
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from fenrir.core.bars import Bar
from fenrir.core.instruments import Instruments, front_month
from fenrir.data.pull import Window

SCHEMA = {
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


@dataclass(frozen=True, slots=True)
class Coverage:
    series: str
    first_day: date | None
    last_day: date | None
    days: int
    gaps: tuple[tuple[date, date], ...]  # missing trading-day ranges (weekends/CME holidays excluded)


# --- pure ---


def trading_day_expr() -> pl.Expr:
    # same rule as fenrir.core.sessions.trading_day: 18:00 New York starts the next trading day
    return (pl.col("ts").dt.convert_time_zone("America/New_York") + pl.duration(hours=6)).dt.date()


def find_gaps(present: set[date], expected: Iterable[date]) -> tuple[tuple[date, date], ...]:
    gaps: list[tuple[date, date]] = []
    run: tuple[date, date] | None = None
    for d in expected:
        if d in present:
            if run:
                gaps.append(run)
            run = None
        else:
            run = (run[0], d) if run else (d, d)
    return tuple(gaps + ([run] if run else []))


def with_contract(
    manifest: Mapping[str, Any], window: Window, rows: int, today: date, pulled_at: str
) -> dict[str, Any]:
    entry = {
        "first_day": window.first.isoformat(),
        "last_day": window.last.isoformat(),
        "rows": rows,
        # yesterday's evening session may still be publishing; only older windows are final
        "complete": window.last < today - timedelta(days=1),
    }
    series = manifest.get("series", {})
    return {
        **manifest,
        "pulled_at": pulled_at,
        "series": {**series, window.series: {**series.get(window.series, {}), window.contract: entry}},
    }


# --- shell ---


def _series_dir(data_dir: Path, series: str) -> Path:
    return data_dir / "bars" / series / "1m"


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / "bars" / "manifest.json"


def _atomic_write(path: Path, write: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    tmp.replace(path)


def read_manifest(data_dir: Path) -> dict[str, Any]:
    path = _manifest_path(data_dir)
    return json.loads(path.read_text()) if path.exists() else {"series": {}}


def write_manifest(data_dir: Path, manifest: Mapping[str, Any]) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True)
    _atomic_write(_manifest_path(data_dir), lambda p: p.write_text(text))


def data_version(data_dir: Path) -> str:
    path = _manifest_path(data_dir)
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def write_contract(data_dir: Path, series: str, contract: str, frame: pl.DataFrame) -> int:
    clean = (
        frame.select(
            pl.col("ts").dt.convert_time_zone("UTC").dt.cast_time_unit("us"),
            *(pl.col(c).cast(t) for c, t in SCHEMA.items() if c != "ts"),
        )
        .unique("ts", keep="last", maintain_order=True)
        .sort("ts")
    )
    _atomic_write(_series_dir(data_dir, series) / f"{contract}.parquet", clean.write_parquet)
    return clean.height


def write_bars(data_dir: Path, series: str, contract: str, bars: Sequence[Bar]) -> None:
    if not bars:
        return
    new = pl.DataFrame(
        {c: [getattr(b, c) for b in bars] for c in SCHEMA},
        schema=SCHEMA,
    )
    path = _series_dir(data_dir, series) / f"{contract}.parquet"
    merged = pl.concat([pl.read_parquet(path), new]) if path.exists() else new
    rows = write_contract(data_dir, series, contract, merged)  # keep="last": new bars win
    days = merged.select(trading_day_expr()).to_series()
    window = Window(series, contract, days.min(), days.max())
    # today=date.max marks the contract complete, so a later `fenrir data pull` leaves it alone
    manifest = with_contract(read_manifest(data_dir), window, rows, date.max, datetime.now(UTC).isoformat())
    write_manifest(data_dir, manifest)


def _read_series(data_dir: Path, series: str) -> pl.DataFrame | None:
    files = sorted(_series_dir(data_dir, series).glob("*.parquet"))
    if not files:
        return None
    return pl.concat(
        [pl.read_parquet(f).with_columns(pl.lit(f.stem).alias("contract")) for f in files]
    ).with_columns(trading_day_expr().alias("day"))


def load_bars(
    data_dir: Path,
    instruments: Instruments,
    series: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[Bar, ...]:
    frame = _read_series(data_dir, series)
    if frame is None:
        return ()
    if start:
        frame = frame.filter(pl.col("day") >= start)
    if end:
        frame = frame.filter(pl.col("day") <= end)
    days = frame["day"].unique().to_list()
    front = pl.DataFrame(
        {"day": days, "contract": [front_month(instruments, series, d) for d in days]},
        schema={"day": pl.Date, "contract": pl.String},
    )
    rows = frame.join(front, on=["day", "contract"]).sort("ts")
    return tuple(
        Bar(ts, o, h, lo, c, v, k)
        for ts, o, h, lo, c, v, k in rows.select(
            "ts", "open", "high", "low", "close", "volume", "contract"
        ).iter_rows()
    )


def expected_days(first: date, last: date) -> Sequence[date]:
    import pandas_market_calendars as mcal

    return [d.date() for d in mcal.get_calendar("CME_Equity").valid_days(first, last)]


def coverage(data_dir: Path, series: str) -> Coverage:
    frame = _read_series(data_dir, series)
    if frame is None or frame.height == 0:
        return Coverage(series, None, None, 0, ())
    days = set(frame["day"].unique().to_list())
    first, last = min(days), max(days)
    return Coverage(series, first, last, len(days), find_gaps(days, expected_days(first, last)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/data/test_store.py`
Expected: `11 passed`.

- [ ] **Step 5: Let `data/CLAUDE.md` be committed and write it**

In `.gitignore`, replace these two lines:

```
data/
!data/.gitkeep
```

with:

```
data/*
!data/CLAUDE.md
```

`data/CLAUDE.md`:

```markdown
# data/

Local runtime data. Gitignored except this file; may be large and licensed. Never commit its contents.

- `bars/{ES,NQ}/1m/<contract>.parquet`: 1-minute bars per contract (`ts` = bar open, UTC). Written by `fenrir data pull`.
- `bars/manifest.json`: what was pulled; its sha256 is the data version used in result caching.
- `results/`: backtest report JSON (plan 10). `jobs/`: backtest job state (plan 14). `fenrir.db`: SQLite journal and armed strategies (plan 21). `gate/`: data-gate script output.
- Read through `fenrir.data.store` (`load_bars`, `coverage`), not by globbing files directly.
- Location set by `FENRIR_DATA_DIR` (default `data`).
```

Check: `git check-ignore -v data/CLAUDE.md; echo $?` → Expected `1` (not ignored). `git check-ignore data/bars/x.parquet` → Expected prints `data/bars/x.parquet` (ignored).

Append to the end of the table in root `CLAUDE.md` → "## 6. Directory Map":

```markdown
| `data/` | Gitignored runtime data: Parquet bars, manifest, backtest results, jobs, SQLite. Only `CLAUDE.md` is committed. |
```

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format . && uv run ruff check .
git add .gitignore data/CLAUDE.md src/fenrir/data/store.py tests/data/test_store.py CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(data): store bars per contract and load front-month series

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: LSE fetch and `fenrir data pull` / `summary`

**Files:**
- Create: `src/fenrir/data/lse.py` (PROVISIONAL)
- Modify: `src/fenrir/data/pull.py` (add `pull`)
- Create: `src/fenrir/cli/data.py`
- Modify: `src/fenrir/cli/main.py`
- Test: `tests/data/test_pull.py` (append), `tests/data/test_lse.py`, `tests/cli/test_data.py`
- Modify: `pyproject.toml`, `uv.lock`

**Interfaces:**
- Consumes: Task 2 and 3 functions; `Settings`, `load_settings` (plan 01); `load_instruments`, `trading_day`, `NY` (plan 01).
- Produces: `pull(data_dir, instruments, series, since, today, fetch, now) -> tuple[Window, ...]`; `fenrir.data.lse.fetch_contract(settings, window) -> pl.DataFrame`; `fenrir.data.lse.lse_symbol(contract) -> str`; CLI `fenrir data pull [--series ES --series NQ] [--since YYYY-MM-DD]`, `fenrir data summary [--json]`. The `summary --json` shape is `{"data_version": str, "series": [{"series", "first_day", "last_day", "days", "gaps": [[first, last], ...]}]}` (plan 16 and the Claude skill read it).

- [ ] **Step 1: Write the failing tests**

In `tests/data/test_pull.py`, replace the import block at the top with:

```python
import json
from datetime import UTC, date, datetime, time

import polars as pl
import pytest

from fenrir.core.instruments import parse_instruments
from fenrir.data.pull import Window, pull, pull_windows, todo
from fenrir.data.store import load_bars
```

and append:

```python
def fake_fetch(w: Window) -> pl.DataFrame:
    ts = datetime.combine(w.last, time(15, 0), tzinfo=UTC)
    return pl.DataFrame({"ts": [ts], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]})


def test_pull_writes_contracts_and_manifest(tmp_path):
    done = pull(tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), fake_fetch, lambda: "t")
    assert [w.contract for w in done] == ["NQZ25", "NQH26", "NQM26"]
    manifest = json.loads((tmp_path / "bars" / "manifest.json").read_text())
    assert sorted(manifest["series"]["NQ"]) == ["NQH26", "NQM26", "NQZ25"]
    assert len(load_bars(tmp_path, INST, "NQ")) == 3
    # second run only re-pulls the incomplete current contract
    again = pull(tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), fake_fetch, lambda: "t")
    assert [w.contract for w in again] == ["NQM26"]


def test_pull_failure_keeps_finished_contracts(tmp_path):
    def flaky(w: Window) -> pl.DataFrame:
        if w.contract == "NQH26":
            raise ConnectionError("dropped")
        return fake_fetch(w)

    with pytest.raises(ConnectionError):
        pull(tmp_path, INST, ("NQ",), date(2025, 12, 1), date(2026, 3, 20), flaky, lambda: "t")
    manifest = json.loads((tmp_path / "bars" / "manifest.json").read_text())
    assert list(manifest["series"]["NQ"]) == ["NQZ25"]
```

`tests/data/test_lse.py`:

```python
from fenrir.data.lse import lse_symbol


def test_lse_symbol_maps_two_digit_year():
    # PROVISIONAL (gate): update to the format recorded in docs/data-gate.md
    assert lse_symbol("NQZ26") == "NQZ6"
    assert lse_symbol("ESH30") == "ESH0"
```

`tests/cli/test_data.py`:

```python
import json
import shutil
from datetime import UTC, datetime, time
from pathlib import Path

import polars as pl
from typer.testing import CliRunner

from fenrir.cli.main import app

REPO = Path(__file__).parents[2]


def setup(tmp_path, monkeypatch, key: str = "test-key"):
    shutil.copy(REPO / "instruments.yaml", tmp_path / "instruments.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FENRIR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FENRIR_LSE_API_KEY", key)


def test_summary_without_data(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["data", "summary"])
    assert result.exit_code == 0
    assert "No bars yet. Run: fenrir data pull" in result.output


def test_pull_requires_key(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, key="")
    result = CliRunner().invoke(app, ["data", "pull"])
    assert result.exit_code == 1
    assert "FENRIR_LSE_API_KEY" in result.output


def test_pull_then_summary_json(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)

    def fake(settings, w):
        ts = datetime.combine(w.last, time(15, 0), tzinfo=UTC)
        return pl.DataFrame({"ts": [ts], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]})

    monkeypatch.setattr("fenrir.cli.data.fetch_contract", fake)
    since = datetime.now(UTC).date().replace(day=1).isoformat()
    pulled = CliRunner().invoke(app, ["data", "pull", "--series", "NQ", "--since", since])
    assert pulled.exit_code == 0, pulled.output
    out = json.loads(CliRunner().invoke(app, ["data", "summary", "--json"]).output)
    assert len(out["data_version"]) == 64
    nq = next(s for s in out["series"] if s["series"] == "NQ")
    assert nq["days"] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/data tests/cli`
Expected: FAIL with `ImportError: cannot import name 'pull' from 'fenrir.data.pull'` and `ModuleNotFoundError: No module named 'fenrir.data.lse'`.

- [ ] **Step 3: Write the implementation**

Replace `src/fenrir/data/pull.py` with (adds `pull`; the store import stays inside `pull` because `fenrir.data.store` imports `Window` from this module):

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from fenrir.core.instruments import Instruments


@dataclass(frozen=True, slots=True)
class Window:
    series: str
    contract: str
    first: date  # first trading day pulled, inclusive
    last: date  # last trading day pulled, inclusive


def pull_windows(instruments: Instruments, series: str, since: date, today: date) -> tuple[Window, ...]:
    rolls = [r for r in instruments.rolls if r.series == series]
    ends = [r.start - timedelta(days=1) for r in rolls[1:]] + [today]
    windows = (
        Window(series, r.contract, max(r.start, since), min(end, today))
        for r, end in zip(rolls, ends, strict=True)
    )
    return tuple(w for w in windows if w.first <= w.last)


def todo(windows: tuple[Window, ...], manifest: Mapping[str, Any], today: date) -> tuple[Window, ...]:
    def done(w: Window) -> bool:
        entry = manifest.get("series", {}).get(w.series, {}).get(w.contract)
        return bool(entry) and entry["complete"] and date.fromisoformat(entry["first_day"]) <= w.first

    return tuple(w for w in windows if not done(w))


def pull(
    data_dir: Path,
    instruments: Instruments,
    series: tuple[str, ...],
    since: date,
    today: date,
    fetch: Callable[[Window], pl.DataFrame],
    now: Callable[[], str],
) -> tuple[Window, ...]:
    from fenrir.data.store import read_manifest, with_contract, write_contract, write_manifest

    manifest = read_manifest(data_dir)
    windows = todo(tuple(w for s in series for w in pull_windows(instruments, s, since, today)), manifest, today)
    for w in windows:
        rows = write_contract(data_dir, w.series, w.contract, fetch(w))
        # manifest after every contract, so an interrupted pull keeps what finished
        manifest = with_contract(manifest, w, rows, today, now())
        write_manifest(data_dir, manifest)
    return windows
```

`src/fenrir/data/lse.py`:

```python
"""The only module that knows about London Strategic Edge.

PROVISIONAL (gate): client, call and field names follow the expected lse-data API.
Align with docs/data-gate.md "Client API" before first real use.
"""

from datetime import UTC, datetime, time, timedelta

import polars as pl

from fenrir.core.sessions import NY
from fenrir.data.pull import Window
from fenrir.data.store import SCHEMA
from fenrir.settings import Settings


def lse_symbol(contract: str) -> str:
    # PROVISIONAL (gate): assumes LSE uses a one-digit year ("NQZ6") for our "NQZ26"
    return contract[:-2] + contract[-1]


def fetch_contract(settings: Settings, window: Window) -> pl.DataFrame:
    from lse_data import Client  # PROVISIONAL (gate)

    start = datetime.combine(window.first - timedelta(days=1), time(18), NY)  # session open
    end = datetime.combine(window.last, time(17), NY)  # session close
    rows = Client(api_key=settings.lse_api_key).history(
        symbol=lse_symbol(window.contract), interval="1m", start=start, end=end
    )
    return pl.DataFrame(
        {
            "ts": [r.timestamp.astimezone(UTC) for r in rows],  # PROVISIONAL (gate): shift if bar-close
            "open": [r.open for r in rows],
            "high": [r.high for r in rows],
            "low": [r.low for r in rows],
            "close": [r.close for r in rows],
            "volume": [r.volume for r in rows],
        },
        schema=SCHEMA,
    )
```

`src/fenrir/cli/data.py`:

```python
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated

import typer

from fenrir.core.instruments import load_instruments
from fenrir.core.sessions import trading_day
from fenrir.data.lse import fetch_contract
from fenrir.data.pull import Window, pull as run_pull
from fenrir.data.store import coverage, data_version
from fenrir.settings import load_settings

DEFAULT_SINCE = "2010-01-01"  # PROVISIONAL (gate): earliest usable day from docs/data-gate.md
SERIES = ("ES", "NQ")

app = typer.Typer(no_args_is_help=True, help="Market data: pull LSE history and show coverage.")


@app.command()
def pull(
    series: Annotated[list[str] | None, typer.Option("--series", help="ES and/or NQ")] = None,
    since: Annotated[str, typer.Option(help="first trading day, YYYY-MM-DD")] = DEFAULT_SINCE,
) -> None:
    """Download 1-minute bars from LSE into data/bars/."""
    settings = load_settings()
    if not settings.lse_api_key:
        typer.echo("FENRIR_LSE_API_KEY is not set. Add it to .env.", err=True)
        raise typer.Exit(1)

    def fetch(w: Window):
        typer.echo(f"{w.contract} {w.first}..{w.last}")
        return fetch_contract(settings, w)

    windows = run_pull(
        settings.data_dir,
        load_instruments(),
        tuple(series or SERIES),
        date.fromisoformat(since),
        trading_day(datetime.now(UTC)),
        fetch,
        lambda: datetime.now(UTC).isoformat(),
    )
    typer.echo(f"pulled {len(windows)} contract windows; data version {data_version(settings.data_dir)[:12]}")


@app.command()
def summary(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show downloaded coverage and gaps per series."""
    settings = load_settings()
    covs = [coverage(settings.data_dir, s) for s in SERIES]
    if as_json:
        out = {"data_version": data_version(settings.data_dir), "series": [asdict(c) for c in covs]}
        typer.echo(json.dumps(out, default=str))
        return
    if all(c.days == 0 for c in covs):
        typer.echo("No bars yet. Run: fenrir data pull")
        return
    for c in covs:
        typer.echo(f"{c.series}  {c.first_day} .. {c.last_day}  {c.days} trading days  {len(c.gaps)} gaps")
        for a, b in c.gaps:
            typer.echo(f"    missing {a} .. {b}")
```

`src/fenrir/cli/main.py` becomes:

```python
import typer

from fenrir.cli import data

app = typer.Typer(no_args_is_help=True)
app.add_typer(data.app, name="data")


@app.callback()
def main() -> None:
    """Fenrir Desk: backtest and run prop-firm futures strategies."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest`
Expected: all pass, including the 6 tests in `test_pull.py`, 11 in `test_store.py`, 1 in `test_lse.py` and 3 in `test_data.py`.

- [ ] **Step 5: Add the client dependency**

```bash
uv add lse-data
uv run python -c "import lse_data; print('ok')"
```

Expected: `ok`. (Use the install and import names recorded in `docs/data-gate.md` if they differ.)

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format . && uv run ruff check .
git add pyproject.toml uv.lock src/fenrir/data src/fenrir/cli tests/data tests/cli
git commit -m "$(cat <<'EOF'
feat(data): add fenrir data pull and summary commands

LSE access is confined to fenrir.data.lse and marked provisional
until it is checked against docs/data-gate.md.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Real pull and check

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything above, a real LSE key in `.env`.
- Produces: `data/bars/` populated for plans 08–10.

- [ ] **Step 1: Pull one recent contract window first**

Run: `uv run fenrir data pull --series NQ --since 2026-09-01`
Expected: one line per contract window (e.g. `NQU26 2026-09-01..2026-09-09`, `NQZ26 2026-09-10..<today>`), then `pulled 2 contract windows; data version <12 hex>`.

- [ ] **Step 2: Spot-check prices against the gate**

```bash
uv run python -c "
from datetime import date
from pathlib import Path
from fenrir.core.instruments import load_instruments
from fenrir.data.store import load_bars
bars = load_bars(Path('data'), load_instruments(), 'NQ', date(2026, 9, 9), date(2026, 9, 10))
print(bars[0]); print(bars[-1]); print(sorted({b.contract for b in bars}))
"
```

Expected: first bar at 22:00 or 23:00 UTC (18:00 New York) on 2026-09-08 with contract `NQU26`; bars from trading day 2026-09-10 have contract `NQZ26`; prices match the NQ range on those days.

- [ ] **Step 3: Full pull and summary**

```bash
uv run fenrir data pull
uv run fenrir data summary
```

Expected: ES and NQ coverage from the default `--since` to today; gaps only where `docs/data-gate.md` recorded real (`G`) gaps. If new gaps appear, record them in `docs/data-gate.md` and tell the user.
