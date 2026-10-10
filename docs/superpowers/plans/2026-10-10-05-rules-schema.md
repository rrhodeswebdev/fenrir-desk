# Rules Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the YAML schema for one prop-firm account type, load and dump account files with strict validation, and ship the Lucid rule files.

**Architecture:** `src/fenrir/rules/schema.py` holds frozen pydantic v2 models (`extra="forbid"`) that mirror spec §4's YAML. `src/fenrir/rules/loader.py` is the thin I/O shell: account id → path → YAML → `AccountRules`, plus listing and dumping. Rule files live in `rules/lucid/` and are data, entered by the user from Lucid's own site.

**Tech Stack:** Python 3.12, `uv`, `pydantic>=2`, `pyyaml`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§4 Rules library)

**Index:** `docs/superpowers/plans/README.md` (contract: "Plan 05 — `fenrir.rules.schema` / `loader`")

## Global Constraints

- Python 3.12, `uv`, src layout, package `fenrir`. Run tests with `uv run pytest -q`.
- Models use `ConfigDict(frozen=True, extra="forbid")`: loading rejects unknown keys at every level.
- One YAML per Lucid account type and size: `rules/lucid/<type>-<size>.yaml`; account id is `lucid-<type>-<size>` (e.g. `lucid-flex-50k`).
- Values are entered from Lucid's own site, not third-party summaries. `verified_on` records when; `null` means not yet verified.
- Lucid Direct has no evaluation: its `evaluation` block is `null` and attempts start in the funded phase.
- Account types in scope: Pro, Flex, Direct, Daily. Instruments: ES, MES, NQ, MNQ.
- Functional core, imperative shell: `schema.py` pure, `loader.py` is the only file that touches disk.
- Commits: Conventional Commits, scope `rules`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Path traversal through an account id** (`lucid-../../etc/passwd` arriving from the UI in plan 15) → rejected before any file is opened. Test: `test_rules_path_rejects_bad_ids` (Task 2).
2. **Unquoted `flat_by: 16:45`** (YAML 1.1 reads it as the integer 1005) → clear error naming `flat_by`, not a silently wrong time. Test: `test_unquoted_flat_by_rejected` (Task 2).
3. **Empty or malformed YAML file** → `ValueError` naming the file, not `TypeError` or a bare YAML traceback. Tests: `test_empty_file_names_path`, `test_malformed_yaml_names_path` (Task 2).
4. **File whose `firm`/`account` don't match its path** → rejected, so `list_accounts` never shows an id that loads different rules. Test: `test_mismatched_id_rejected` (Task 2).
5. **UI edit round trip** (plan 15 writes `dump_account` output back to disk) → reloading gives an equal model. Test: `test_dump_round_trip` (Task 2).

---

### Task 1: Account rules schema

**Files:**
- Create: `src/fenrir/rules/__init__.py`
- Create: `src/fenrir/rules/schema.py`
- Create: `src/fenrir/rules/CLAUDE.md`
- Create: `tests/rules/conftest.py`
- Create: `tests/rules/test_schema.py`
- Modify: `CLAUDE.md` (§6 Directory Map table)
- Modify: `pyproject.toml` (via `uv add`)

**Interfaces:**
- Consumes: plan 01's `pyproject.toml` and pytest setup.
- Produces (`fenrir.rules.schema`): `AccountRules`, `Drawdown`, `Consistency`, `Payout`, `Phase`, `Session`, `Contracts`, `Microscalp`, `Prohibited`, `Costs`, `SYMBOLS`. Fields as in the code below. `Phase` carries both `profit_target` (evaluation only) and `payout` (funded only); `AccountRules` validates that pairing. Test fixture `base` (a fresh, valid rules `dict`) in `tests/rules/conftest.py`, reused by plan 06.

- [ ] **Step 1: Add dependencies**

Run: `uv add pydantic pyyaml`
Expected: `pyproject.toml` lists `pydantic>=2...` and `pyyaml>=...` under `dependencies` (pyyaml may already be there from plan 01; `uv add` leaves it unchanged).

- [ ] **Step 2: Write the shared test fixture**

`tests/rules/conftest.py`:

```python
import copy

import pytest

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


@pytest.fixture
def base() -> dict:
    """A fresh, valid account-rules dict; tests mutate it freely."""
    return copy.deepcopy(BASE)
```

- [ ] **Step 3: Write the failing tests**

`tests/rules/test_schema.py`:

```python
import pytest
from pydantic import ValidationError

from fenrir.rules.schema import AccountRules


def test_base_parses(base):
    rules = AccountRules.model_validate(base)
    assert rules.funded.payout.split_pct == 90
    assert rules.evaluation.max_drawdown.trailing == "end_of_day"
    assert rules.evaluation.profit_target == 3000.0


def test_locks_at_defaults_to_none(base):
    del base["evaluation"]["max_drawdown"]["locks_at"]
    assert AccountRules.model_validate(base).evaluation.max_drawdown.locks_at is None


def test_unknown_top_level_key_rejected(base):
    base["news_blackout"] = True
    with pytest.raises(ValidationError, match="news_blackout"):
        AccountRules.model_validate(base)


def test_unknown_nested_key_rejected(base):
    base["funded"]["payout"]["buffer"] = 100
    with pytest.raises(ValidationError, match="buffer"):
        AccountRules.model_validate(base)


def test_direct_has_no_evaluation(base):
    base["evaluation"] = None
    assert AccountRules.model_validate(base).evaluation is None


def test_evaluation_needs_profit_target(base):
    base["evaluation"]["profit_target"] = None
    with pytest.raises(ValidationError, match="profit_target"):
        AccountRules.model_validate(base)


def test_funded_needs_payout(base):
    del base["funded"]["payout"]
    with pytest.raises(ValidationError, match="payout"):
        AccountRules.model_validate(base)


def test_negative_drawdown_rejected(base):
    base["funded"]["max_drawdown"]["amount"] = -2000
    with pytest.raises(ValidationError, match="amount"):
        AccountRules.model_validate(base)


def test_bad_flat_by_rejected(base):
    base["session"]["flat_by"] = "4:45pm"
    with pytest.raises(ValidationError, match="flat_by"):
        AccountRules.model_validate(base)


def test_unknown_time_zone_rejected(base):
    base["session"]["tz"] = "Mars/Olympus"
    with pytest.raises(ValidationError, match="time zone"):
        AccountRules.model_validate(base)


def test_commission_needs_every_symbol(base):
    del base["costs"]["commission_per_side"]["MNQ"]
    with pytest.raises(ValidationError, match="MNQ"):
        AccountRules.model_validate(base)


def test_model_is_frozen(base):
    rules = AccountRules.model_validate(base)
    with pytest.raises(ValidationError):
        rules.firm = "other"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules'`

- [ ] **Step 5: Write the schema**

`src/fenrir/rules/__init__.py`: empty file.

`src/fenrir/rules/schema.py`:

```python
"""Pydantic models for one prop-firm account type's rules (spec §4). Pure: no I/O."""

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SYMBOLS = frozenset({"ES", "MES", "NQ", "MNQ"})

Pct = Annotated[float, Field(gt=0, le=100)]
Money = Annotated[float, Field(ge=0)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Drawdown(_Model):
    amount: float = Field(gt=0)
    trailing: Literal["end_of_day", "intraday"]
    locks_at: float | None = Field(default=None, gt=0)  # profit above start where the floor stops trailing


class Consistency(_Model):
    max_day_pct: Pct


class Payout(_Model):
    max_pct_of_profit: Pct
    split_pct: Pct
    min_days: int = Field(ge=0)
    min_profit: Money


class Phase(_Model):
    max_drawdown: Drawdown
    daily_loss_limit: float | None = Field(default=None, gt=0)
    consistency: Consistency | None = None
    min_trading_days: int = Field(default=0, ge=0)
    profit_target: float | None = Field(default=None, gt=0)  # evaluation only
    payout: Payout | None = None  # funded only


class Session(_Model):
    flat_by: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    tz: str

    @field_validator("tz")
    @classmethod
    def _known_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}") from None
        return value


class Contracts(_Model):
    max_minis: int = Field(gt=0)
    micros_per_mini: int = Field(gt=0)


class Microscalp(_Model):
    max_profit_pct: Pct
    hold_seconds: int = Field(gt=0)


class Prohibited(_Model):
    microscalp: Microscalp | None = None
    hft: bool


class Costs(_Model):
    eval_fee: Money
    activation_fee: Money
    commission_per_side: dict[str, float]

    @field_validator("commission_per_side")
    @classmethod
    def _every_symbol(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != SYMBOLS:
            raise ValueError(f"commission_per_side needs exactly {sorted(SYMBOLS)}, got {sorted(value)}")
        if any(c < 0 for c in value.values()):
            raise ValueError("commission_per_side values must be >= 0")
        return value


class AccountRules(_Model):
    firm: str = Field(pattern=r"^[a-z0-9]+$")
    account: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    verified_on: date | None  # None = not yet verified against the firm's site
    source_url: str
    automation_allowed: bool
    evaluation: Phase | None  # None for accounts with no evaluation (Lucid Direct)
    funded: Phase
    session: Session
    contracts: Contracts
    prohibited: Prohibited
    costs: Costs

    @model_validator(mode="after")
    def _phase_shapes(self) -> "AccountRules":
        e, f = self.evaluation, self.funded
        if e is not None and (e.profit_target is None or e.payout is not None):
            raise ValueError("evaluation needs profit_target and no payout")
        if f.payout is None or f.profit_target is not None:
            raise ValueError("funded needs payout and no profit_target")
        return self
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/rules/test_schema.py -q`
Expected: PASS — `12 passed`

- [ ] **Step 7: Add the directory context file and map row**

`src/fenrir/rules/CLAUDE.md`:

```markdown
# src/fenrir/rules/

Prop-firm account rules: the YAML schema, loading account files, and (plan 06) the pure rule functions that judge an account's state.

- Entry points: `schema.py` (`AccountRules` and its sub-models), `loader.py` (`load_account`, `list_accounts`, `rules_path`, `dump_account`, `account_id`).
- Pure vs shell: `schema.py` is pure; `loader.py` reads files (shell).
- Not here: the YAML files themselves (`rules/lucid/`), contract specs (`src/fenrir/core/instruments.py`).
- Tests: `tests/rules/` (`conftest.py` has the `base` rules-dict fixture).
```

In root `CLAUDE.md`, add this row to the §6 Directory Map table (after the existing rows):

```markdown
| `src/fenrir/rules/` | Rules schema, loader, account state and pure rule functions. |
```

- [ ] **Step 8: Lint**

Run: `uv run ruff check src/fenrir/rules tests/rules && uv run ruff format --check src/fenrir/rules tests/rules`
Expected: `All checks passed!` and no files to reformat (run `uv run ruff format src/fenrir/rules tests/rules` if needed).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/fenrir/rules tests/rules CLAUDE.md
git commit -m "feat(rules): add account rules schema

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Loader, listing and dump

**Files:**
- Create: `src/fenrir/rules/loader.py`
- Create: `tests/rules/test_loader.py`

**Interfaces:**
- Consumes: `AccountRules` (Task 1).
- Produces (`fenrir.rules.loader`), exactly as in the index:
  - `account_id(rules: AccountRules) -> str` — `f"{firm}-{account}"`
  - `rules_path(account_id: str, rules_dir: Path) -> Path` — `ValueError` on an id that is not `^[a-z0-9]+-[a-z0-9]+(-[a-z0-9]+)*$`
  - `load_account(account_id: str, rules_dir: Path) -> AccountRules` — `ValueError` with the path and the parse/validation message
  - `list_accounts(rules_dir: Path) -> tuple[str, ...]` — sorted ids from `rules_dir/*/*.yaml`
  - `dump_account(rules: AccountRules) -> str` — YAML text in schema field order

- [ ] **Step 1: Write the failing tests**

`tests/rules/test_loader.py`:

```python
from pathlib import Path

import pytest
import yaml

from fenrir.rules.loader import account_id, dump_account, list_accounts, load_account, rules_path


def write(rules_dir: Path, data: dict, rel: str = "lucid/flex-50k.yaml") -> Path:
    path = rules_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_rules_path(tmp_path):
    assert rules_path("lucid-flex-50k", tmp_path) == tmp_path / "lucid" / "flex-50k.yaml"


@pytest.mark.parametrize("bad", ["lucid-../x", "../lucid-flex", "Lucid-Flex-50k", "lucid"])
def test_rules_path_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(ValueError, match="invalid account id"):
        rules_path(bad, tmp_path)


def test_load_account(tmp_path, base):
    write(tmp_path, base)
    rules = load_account("lucid-flex-50k", tmp_path)
    assert rules.contracts.max_minis == 4


def test_account_id(tmp_path, base):
    write(tmp_path, base)
    assert account_id(load_account("lucid-flex-50k", tmp_path)) == "lucid-flex-50k"


def test_unknown_account(tmp_path):
    with pytest.raises(ValueError, match="unknown account 'lucid-pro-50k'"):
        load_account("lucid-pro-50k", tmp_path)


def test_empty_file_names_path(tmp_path):
    path = tmp_path / "lucid" / "flex-50k.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    with pytest.raises(ValueError, match="flex-50k.yaml"):
        load_account("lucid-flex-50k", tmp_path)


def test_malformed_yaml_names_path(tmp_path):
    path = tmp_path / "lucid" / "flex-50k.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("firm: lucid\n\taccount: [flex")
    with pytest.raises(ValueError, match="flex-50k.yaml"):
        load_account("lucid-flex-50k", tmp_path)


def test_unquoted_flat_by_rejected(tmp_path, base):
    path = write(tmp_path, base)
    path.write_text(path.read_text().replace("'16:45'", "16:45"))  # YAML 1.1 reads 16:45 as 1005
    with pytest.raises(ValueError, match="flat_by"):
        load_account("lucid-flex-50k", tmp_path)


def test_mismatched_id_rejected(tmp_path, base):
    write(tmp_path, base, rel="lucid/pro-50k.yaml")
    with pytest.raises(ValueError, match="says lucid-flex-50k"):
        load_account("lucid-pro-50k", tmp_path)


def test_list_accounts_sorted(tmp_path, base):
    write(tmp_path, base, rel="lucid/pro-50k.yaml")
    write(tmp_path, base, rel="lucid/flex-50k.yaml")
    assert list_accounts(tmp_path) == ("lucid-flex-50k", "lucid-pro-50k")


def test_dump_round_trip(tmp_path, base):
    write(tmp_path, base)
    rules = load_account("lucid-flex-50k", tmp_path)
    (tmp_path / "lucid" / "flex-50k.yaml").write_text(dump_account(rules))
    assert load_account("lucid-flex-50k", tmp_path) == rules
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/rules/test_loader.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fenrir.rules.loader'`

- [ ] **Step 3: Write the loader**

`src/fenrir/rules/loader.py`:

```python
"""Account id <-> rules file. The only rules code that touches disk."""

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from fenrir.rules.schema import AccountRules

_ID = re.compile(r"([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)")


def account_id(rules: AccountRules) -> str:
    return f"{rules.firm}-{rules.account}"


def rules_path(account_id: str, rules_dir: Path) -> Path:
    match = _ID.fullmatch(account_id)
    if match is None:
        raise ValueError(f"invalid account id {account_id!r}")
    return rules_dir / match[1] / f"{match[2]}.yaml"


def load_account(account_id: str, rules_dir: Path) -> AccountRules:
    path = rules_path(account_id, rules_dir)
    try:
        rules = AccountRules.model_validate(yaml.safe_load(path.read_text()))
    except FileNotFoundError:
        raise ValueError(f"unknown account {account_id!r}: {path} not found") from None
    except (yaml.YAMLError, ValidationError) as e:
        raise ValueError(f"{path}: {e}") from e
    found = f"{rules.firm}-{rules.account}"
    if found != account_id:
        raise ValueError(f"{path}: file says {found}, path says {account_id}")
    return rules


def list_accounts(rules_dir: Path) -> tuple[str, ...]:
    return tuple(sorted(f"{p.parent.name}-{p.stem}" for p in rules_dir.glob("*/*.yaml")))


def dump_account(rules: AccountRules) -> str:
    return yaml.safe_dump(rules.model_dump(), sort_keys=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/rules -q`
Expected: PASS — `26 passed`

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/fenrir/rules tests/rules && uv run ruff format --check src/fenrir/rules tests/rules`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/fenrir/rules/loader.py tests/rules/test_loader.py
git commit -m "feat(rules): load, list and dump account rule files

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Example rules file and every-file test

**Files:**
- Create: `rules/lucid/flex-50k.yaml`
- Create: `rules/CLAUDE.md`
- Create: `tests/rules/test_rules_files.py`
- Modify: `CLAUDE.md` (§6 Directory Map table)

**Interfaces:**
- Consumes: `list_accounts`, `load_account` (Task 2).
- Produces: `rules/lucid/flex-50k.yaml` (account id `lucid-flex-50k`, **unverified** placeholder values copied from spec §4) so plans 06–10 have a real file to load; a test that loads every file under `rules/`.

- [ ] **Step 1: Write the failing test**

`tests/rules/test_rules_files.py`:

```python
from pathlib import Path

import pytest

from fenrir.rules.loader import list_accounts, load_account

RULES = Path(__file__).parents[2] / "rules"


def test_rules_dir_has_files():
    assert list_accounts(RULES)


@pytest.mark.parametrize("account", list_accounts(RULES))
def test_every_rules_file_loads(account):
    load_account(account, RULES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/rules/test_rules_files.py -q`
Expected: FAIL — `test_rules_dir_has_files` fails with `assert ()`

- [ ] **Step 3: Write the example file**

`rules/lucid/flex-50k.yaml`:

```yaml
# UNVERIFIED: illustrative values copied from the design spec, not from Lucid.
# Replace every value from Lucid's own site and set verified_on (plan 05, Task 4).
firm: lucid
account: flex-50k
verified_on: null
source_url: https://lucidtrading.com
automation_allowed: true
evaluation:
  profit_target: 3000
  max_drawdown: { amount: 2000, trailing: end_of_day, locks_at: null }
  daily_loss_limit: null
  consistency: null            # or { max_day_pct: 40 }
  min_trading_days: 0
funded:
  max_drawdown: { amount: 2000, trailing: end_of_day, locks_at: null }
  daily_loss_limit: null
  consistency: null
  payout: { max_pct_of_profit: 50, split_pct: 90, min_days: 0, min_profit: 0 }
session: { flat_by: "16:45", tz: America/New_York }
contracts: { max_minis: 4, micros_per_mini: 10 }
prohibited:
  microscalp: { max_profit_pct: 50, hold_seconds: 5 }
  hft: true
costs: { eval_fee: 0, activation_fee: 0, commission_per_side: { ES: 0, MES: 0, NQ: 0, MNQ: 0 } }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/rules -q`
Expected: PASS — `28 passed`

- [ ] **Step 5: Add the directory context file and map row**

`rules/CLAUDE.md`:

```markdown
# rules/

Prop-firm rule files: one YAML per account type and size at `rules/<firm>/<type>-<size>.yaml`, account id `<firm>-<type>-<size>` (e.g. `lucid-flex-50k`).

- Entry points: `lucid/*.yaml`. Schema and loader: `src/fenrir/rules/schema.py`, `src/fenrir/rules/loader.py`.
- Values come from the firm's own site only, never third-party summaries. Set `verified_on` and `source_url` when you check them; `verified_on: null` means not yet verified.
- Quote times (`flat_by: "16:45"`): unquoted YAML reads 16:45 as a number.
- Unknown keys are rejected. Lucid Direct has `evaluation: null`. `locks_at` is the profit above start where the trailing floor stops moving (`null` = never).
- Not here: rule logic (`src/fenrir/rules/`), contract specs (`instruments.yaml`).
- Tests: `tests/rules/test_rules_files.py` loads every file.
```

In root `CLAUDE.md`, add this row to the §6 Directory Map table:

```markdown
| `rules/` | Prop-firm rule YAML files, one per account type and size, entered from the firm's own site. |
```

- [ ] **Step 6: Commit**

```bash
git add rules tests/rules/test_rules_files.py CLAUDE.md
git commit -m "feat(rules): add unverified lucid flex-50k example rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Enter Lucid's rules (user task)

**This task is for the user, not an agent.** Values must come from Lucid's own site (spec §4). An agent must not fill in numbers from memory, search snippets or third-party summaries. An agent executing this plan stops here, shows the user the checklist below, and waits.

**Files:**
- Create/Modify: `rules/lucid/<type>-<size>.yaml`, one per account Lucid sells
- Test: `tests/rules/test_rules_files.py` (already loads every file)

**Interfaces:**
- Consumes: schema (Task 1), `rules/lucid/flex-50k.yaml` as the shape to copy.
- Produces: verified rule files that plans 06–10 and 23 load by id.

- [ ] **Step 1: List the accounts Lucid currently sells**

On Lucid's site, note every account type (expected: Pro, Flex, Direct, Daily) and every size each one comes in. Create one file per type and size, named `rules/lucid/<type>-<size>.yaml` in lowercase (`pro-25k`, `flex-50k`, `direct-100k`, `daily-150k`). Copy `flex-50k.yaml` as the starting shape. Delete `flex-50k.yaml` if Lucid does not sell Flex 50K.

- [ ] **Step 2: Fill each file from Lucid's site**

For each file, check off:

- [ ] `account` matches the file name; `source_url` is the Lucid page the values came from
- [ ] `evaluation.profit_target`, or `evaluation: null` for Direct
- [ ] `max_drawdown.amount`, `trailing` (`end_of_day` or `intraday`) and `locks_at` (profit above start at which the floor stops trailing; `null` if it never locks), for evaluation and funded
- [ ] `daily_loss_limit` per phase (`null` if none)
- [ ] `consistency.max_day_pct` per phase (`null` if none)
- [ ] `min_trading_days` (evaluation)
- [ ] `funded.payout`: `max_pct_of_profit`, `split_pct`, `min_days`, `min_profit`
- [ ] `session.flat_by` (quoted, e.g. `"16:45"`) and `tz`
- [ ] `contracts.max_minis` and `micros_per_mini`
- [ ] `prohibited.microscalp` and `hft`; `automation_allowed`
- [ ] `costs.eval_fee`, `activation_fee`, `commission_per_side` for ES, MES, NQ, MNQ
- [ ] `verified_on` set to today's date, and the `# UNVERIFIED` header comment removed

If a Lucid rule does not fit the schema (for example a payout buffer, a scaling plan, or a consistency rule measured against the profit target), write it down and raise it before changing the schema. Do not drop it silently.

- [ ] **Step 3: Verify every file loads and none is left unverified**

Run: `uv run pytest tests/rules/test_rules_files.py -q`
Expected: PASS, one `test_every_rules_file_loads` case per file.

Run: `grep -L "verified_on: 20" rules/lucid/*.yaml`
Expected: no output (every file has a `verified_on` date).

- [ ] **Step 4: Commit**

```bash
git add rules/lucid
git commit -m "feat(rules): add verified lucid account rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
