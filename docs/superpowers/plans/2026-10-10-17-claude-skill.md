# Claude Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project skill, `.claude/skills/fenrir-strategy/SKILL.md`, that teaches Claude Code (running in the Workbench terminal) the strategy loop: read the account's rules → inspect data → write or edit a strategy → `fenrir backtest` → read the JSON report → iterate, tuning on the tuning period only and never arming.

**Architecture:** One Markdown skill file plus a pytest that keeps it honest: every `fenrir ...` command it names must exist in the CLI, and its example strategy must load with `load_strategy`.

**Tech Stack:** Claude Code skills (Markdown + YAML frontmatter), Typer `CliRunner`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md` (§7 project skill, arming is user-only; §5 strategies; §6 hold-out)

**Index:** `docs/superpowers/plans/README.md` (contracts: "Plan 01", "Plan 03", "Plan 05", "Plan 07", "Plans 09/10")

## Global Constraints

- Path: `.claude/skills/fenrir-strategy/SKILL.md`.
- The loop (§7): read the target account's rules → inspect data (`fenrir data summary`, ad-hoc analysis) → write or edit a strategy → `fenrir backtest` → read the JSON report → iterate.
- Tune on the tuning period only. Hold-out results are never used to choose or change a strategy.
- No CLI command arms or disarms; arming is user-only from the UI.
- `step` must be pure: no I/O, no clock, no randomness. Every entry is a `Bracket` with a stop.

## Review Focus

1. **Skill names a command or flag the CLI does not have** (drift after a later plan renames something) — Claude would loop on errors. Test: `test_every_fenrir_command_in_skill_exists` (Task 1).
2. **Example strategy no longer matches the strategy contract** — Claude copies a broken skeleton. Test: `test_example_strategy_loads` (Task 1).
3. **Claude peeks at hold-out results** by running `fenrir evaluate` mid-tuning — the skill must say when `evaluate` is allowed (only when the user asks, after tuning is frozen). Checked by `test_skill_forbids_tuning_on_holdout` (Task 1), which asserts the rule text is present.
4. **Claude tries to arm** via `curl` to the API — the skill must say arming is user-only and the terminal has no token. Checked by `test_skill_says_arming_is_user_only` (Task 1).
5. **Non-deterministic strategy** (`datetime.now()`, `random`, file reads in `step`) — backtest and live diverge silently. The skill states the purity rules; plan 07's loader is where enforcement lives, so this plan only pins the wording in `test_skill_states_purity_rules` (Task 1).

---

### Task 1: Write the skill and its drift tests

**Files:**
- Create: `.claude/skills/fenrir-strategy/SKILL.md`
- Create: `.claude/skills/fenrir-strategy/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)
- Test: `tests/test_fenrir_skill.py`

**Interfaces:**
- Consumes: CLI commands `fenrir data summary`, `fenrir backtest`, `fenrir evaluate` (plans 03, 09); `load_strategy` (plan 07); `Bracket`, `Flatten` (`fenrir.core.actions`, plan 01); `trading_day` (`fenrir.core.sessions`, plan 01); `load_bars`, `load_instruments` (plans 03, 01).
- Produces: the skill file; nothing imported by code.

- [ ] **Step 1: Write the failing tests**

`tests/test_fenrir_skill.py`:

```python
import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fenrir.cli.main import app
from fenrir.strategy.loader import load_strategy

SKILL = Path(".claude/skills/fenrir-strategy/SKILL.md")


def text() -> str:
    return SKILL.read_text()


def test_frontmatter():
    front = yaml.safe_load(text().split("---")[1])
    assert front["name"] == "fenrir-strategy"
    assert "strategy" in front["description"]


def test_every_fenrir_command_in_skill_exists():
    commands = set(re.findall(r"`fenrir ((?:[a-z]+ ?)+)", text()))
    assert commands, "skill names no fenrir commands"
    for words in commands:
        argv = [w for w in words.split() if w][:2]
        result = CliRunner().invoke(app, [*argv, "--help"])
        if result.exit_code != 0:  # second word may be a positional argument, not a subcommand
            result = CliRunner().invoke(app, [argv[0], "--help"])
        assert result.exit_code == 0, f"`fenrir {words}` does not exist: {result.output}"


def test_example_strategy_loads(tmp_path):
    code = re.search(r"```python\n(# strategies/example\.py.*?)```", text(), re.S).group(1)
    path = tmp_path / "example.py"
    path.write_text(code)
    strategy = load_strategy(path)
    assert strategy.market == "NQ"
    strategy.init(strategy.params)


def test_skill_forbids_tuning_on_holdout():
    assert "Never tune on the hold-out" in text()


def test_skill_says_arming_is_user_only():
    assert "Never arm" in text()


def test_skill_states_purity_rules():
    assert "no I/O, no clock, no randomness" in text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fenrir_skill.py -q`
Expected: FAIL with `FileNotFoundError: ... .claude/skills/fenrir-strategy/SKILL.md`

- [ ] **Step 3: Write the skill**

`.claude/skills/fenrir-strategy/SKILL.md`:

````markdown
---
name: fenrir-strategy
description: Use when designing, writing, editing, tuning or backtesting a Fenrir Desk futures strategy for a Lucid prop account, or when reading a backtest report or the live trade journal.
---

# Fenrir strategy loop

You write deterministic strategy code whose only goal is passing a Lucid evaluation and reaching a first payout. The app backtests it against the firm's rules; the user alone decides whether it trades live.

## Hard rules

- **Never arm or disarm a strategy.** Arming is user-only, from the UI's Strategies page. There is no CLI command for it, and this terminal has no session token. Do not try `curl` against the API.
- **Never tune on the hold-out.** Use `fenrir backtest` (tuning period only) while designing. Run `fenrir evaluate` only when the user asks, after tuning is finished. Never change a strategy, its parameters or its idea because of hold-out results; report them instead.
- **`step` must be pure: no I/O, no clock, no randomness.** No `datetime.now()`, `random`, file or network access, globals that change, or mutation of `state`. Time comes from `bar.ts` / `ctx.ts`. The same file runs in backtests and live.
- **Every entry has a stop.** The only entry action is `Bracket(side, qty, stop, target)`.
- Edit only `strategies/*.py` unless the user asks for something else.

## The loop

1. **Read the target account's rules.** `rules/lucid/<type>-<size>.yaml` (account id `lucid-<type>-<size>`, e.g. `lucid-flex-50k`). Note the profit target, drawdown amount and whether it trails end-of-day or intraday, daily loss limit, consistency rule, `flat_by` time, contract limits (`max_minis`, `micros_per_mini`) and fees. These are hard limits; design for them.
2. **Inspect data.** `fenrir data summary` shows coverage and gaps for ES and NQ. For ad-hoc analysis load bars in Python:

   ```bash
   uv run python -c "
   from datetime import date
   from pathlib import Path
   from fenrir.core.instruments import load_instruments
   from fenrir.data.store import load_bars
   bars = load_bars(Path('data'), load_instruments(), 'NQ', date(2024, 1, 2), date(2024, 1, 31))
   print(len(bars), bars[0], bars[-1])
   "
   ```

   Do not read data from the hold-out window (the most recent `FENRIR_HOLDOUT_MONTHS`, default 12 months) when forming ideas.
3. **Write or edit a strategy** in `strategies/<name>.py` (see the contract below; `strategies/orb.py` is a full example).
4. **Backtest.** `fenrir backtest strategies/<name>.py --account lucid-flex-50k --json`. Grids: `fenrir backtest strategies/*.py --account 'lucid-*'`, `--contract MNQ`. Results are cached by file hash, so re-running an unchanged file is instant.
5. **Read the JSON report.** Optimize `periods.tuning.p_pass_and_payout` and `periods.tuning.expected_net_per_attempt`, not raw profit. Read `failure_reasons` to see which rule kills attempts, `days_to_pass` for speed, and `microscalp_flags` (trades that open and close inside one bar; Lucid prohibits microscalping — fix any non-zero count).
6. **Iterate** on the idea and `PARAMS`, then back to step 4. Keep a short log for the user of what you tried and what each change did to the tuning numbers.

## Strategy contract

A strategy is one file in `strategies/` exporting `MARKET`, `PARAMS`, `init` and `step`:

```python
# strategies/example.py
from dataclasses import dataclass, replace
from datetime import date

from fenrir.core.actions import Bracket
from fenrir.core.sessions import trading_day

MARKET = "NQ"  # price series: "ES" or "NQ"; the contract (NQ or MNQ) is chosen per run
PARAMS = {"stop_ticks": 40, "target_ticks": 80}


@dataclass(frozen=True)
class State:
    stop_ticks: int
    target_ticks: int
    last_trade_day: date | None = None


def init(params):
    return State(params["stop_ticks"], params["target_ticks"])


def step(state, bar, ctx):
    day = trading_day(bar.ts)
    if ctx.position.qty != 0 or state.last_trade_day == day:
        return state, []
    tick = ctx.spec.tick
    entry = Bracket("long", 1, bar.close - state.stop_ticks * tick, bar.close + state.target_ticks * tick)
    return replace(state, last_trade_day=day), [entry]
```

- `step(state, bar, ctx) -> (new_state, actions)` runs once per closed 1-minute bar.
- `bar`: `ts` (open time, UTC), `open`, `high`, `low`, `close`, `volume`, `contract` (unadjusted front month, e.g. `NQZ26`).
- `ctx`: `ts`; `bars` (up to 500 recent bars ending with this one); `position` (`qty` signed, `entry`, `stop`, `target`); `account` (`phase`, `drawdown_remaining`, `daily_loss_remaining`, `today_pnl`, `target_remaining`); `spec` (`tick`, `point_value`, `minis`).
- Actions (`fenrir.core.actions`): `Bracket(side, qty, stop, target)` with absolute prices, `Flatten()`, `Cancel(order_id)`.
- Fills are conservative: entry at the next bar's open plus 1 tick; if stop and target are both inside one bar the stop fills first; stops and market exits get 1 tick slippage.
- A bracket whose stop-out would breach the drawdown, the daily loss limit or the contract limit is rejected, in backtests and live alike. Size so the stop fits inside `ctx.account.daily_loss_remaining` and `drawdown_remaining`.
- Positions are flattened at the account's `flat_by` time; do not plan to hold overnight.

## Comparing live with backtests

When the user asks how live trading compares, read the journal in `data/fenrir.db` (table `journal`: `ts, account, kind, payload_json`, kinds `bar`, `action`, `rejection`, `fill`, `alert`, `halt`) with `sqlite3` or Python, and compare fills and decisions with a backtest over the same days. Read only; never write to it.
````

`.claude/skills/fenrir-strategy/CLAUDE.md`:

```markdown
# .claude/skills/fenrir-strategy/

The project skill Claude Code loads when working on strategies. `SKILL.md` is the content; it teaches the rules → data → strategy → `fenrir backtest` → report loop.

- Keep it in step with the CLI and the strategy contract: `tests/test_fenrir_skill.py` fails when a named command or the example strategy drifts.
- Not here: strategy files (`strategies/`), rules (`rules/lucid/`).
```

Add this row to the root `CLAUDE.md` Directory Map table:

```markdown
| `.claude/skills/fenrir-strategy/` | Project skill teaching Claude the strategy loop (rules → data → strategy → backtest → report). |
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fenrir_skill.py -q`
Expected: `6 passed`

- [ ] **Step 5: Try the skill**

Run `claude` in the repo root and ask: "Write a strategy for lucid-flex-50k and backtest it." Expected: Claude announces the `fenrir-strategy` skill, reads `rules/lucid/flex-50k.yaml`, runs `fenrir data summary`, writes `strategies/<name>.py`, runs `fenrir backtest ... --json`, and reports tuning numbers without running `fenrir evaluate`.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/fenrir-strategy tests/test_fenrir_skill.py CLAUDE.md
git commit -m "feat(skill): add fenrir-strategy skill for the strategy loop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
