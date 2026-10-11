---
name: fenrir-strategy
description: Use when designing, writing, editing, tuning or backtesting a Fenrir Desk futures strategy for a Lucid prop account, or when reading a Fenrir backtest report.
---

# Fenrir strategy loop

You write deterministic strategy code whose only goal is passing a Lucid evaluation and reaching a first payout, not raw profit. The app backtests it against the firm's rules; only the user decides whether it trades live.

## Hard rules

- **Never arm or disarm a strategy.** Arming is user-only, from the UI. No CLI command arms; do not try the API with `curl` or anything else.
- **Never read `.env`, the session key or anything under `data/private/`.** In Docker the terminal cannot; running natively it can, so this rule is the only guard.
- **Never tune on the hold-out.** Use `uv run fenrir backtest` (tuning period only) while designing. Run `uv run fenrir evaluate` once, at the end, when tuning is frozen or the user asks. Never change a strategy, its idea or its `PARAMS` because of hold-out results; report them instead.
- **`step` must be pure: no I/O, no clock, no randomness.** No `datetime.now()`, `random`, files or network. Time comes from `bar.ts` / `ctx.ts`. The loader only blocks a fixed list of imports (`os`, `time`, `random`, ...) and `.now()`/`.today()` calls; it is a static check, not a sandbox, so purity is on you.
- **Every entry has a stop.** The only entry action is `Bracket(side, qty, stop, target)`.
- Edit only `strategies/*.py` unless the user asks for something else.

## The loop

1. **Read the account's rules.** `rules/lucid/<type>-<size>.yaml` (account id `lucid-<type>-<size>`, e.g. `lucid-flex-50k`). Note the profit target, drawdown amount and whether it trails end-of-day or intraday, consistency, `flat_by`, contract limits and costs. The `daily_loss_limit` applies only when the user turned the DLL on for that account (`dll_on`, shown in every report); otherwise there is none. Semantics: `docs/lucid-mechanics.md`.
2. **Inspect data coverage.** `uv run fenrir data summary --json` shows coverage and gaps for ES and NQ. Do not form ideas from the hold-out window (the most recent `holdout_months`, 12 by default, as shown in every report).
3. **Write the strategy** in `strategies/<name>.py` per the contract below (full example: `strategies/orb.py`; contract: `strategies/CLAUDE.md`).
4. **Backtest the tuning period.** `uv run fenrir backtest 'strategies/<name>.py' --account 'lucid-flex-50k' --json`. Grids take several strategy globs, account globs and contracts: `uv run fenrir backtest 'strategies/*.py' --account 'lucid-*' --contract MNQ --json` (contract repeatable; default both contracts of `MARKET`). Quote globs. Results are cached by file hash, so re-running an unchanged file is instant.
5. **Read the report** (`periods.tuning`). Optimize `p_pass_and_payout` and `expected_gross_per_attempt`; use `failure_reasons` (`max_drawdown`, `flat_by`, `no_result`) to see what ends attempts, `days_to_pass` for speed, and fix any non-zero `microscalp_flags` (open and close in one bar). Rates are estimates: attempts start every trading day and overlap.
6. **Iterate** on the idea and `PARAMS`, back to step 4, tuning period only. Keep a short log for the user of what each change did. `uv run fenrir compare --view strategy --json` ranks the latest cached reports by tuning results.
7. **Hold-out check, last.** `uv run fenrir evaluate 'strategies/<name>.py' --account 'lucid-flex-50k' --json` adds `periods.holdout`. `holdout_gap: true` means held-out `p_pass_and_payout` is under 50% of tuning's: likely overfit. Report it; do not tune to fix it.

## How the engine behaves

- **Fills are conservative:** entry at the next bar's open plus 1 tick; if stop and target are both in one bar, the stop fills first; stops and market exits get 1 tick slippage.
- **A new `Bracket` is rejected while a position is open,** and so is one whose stop-out would breach the drawdown, the daily loss limit or the contract limit. Rejections are silent: also when another bracket is still pending (it fills on the next bar), when stop and target are not on either side of this bar's close (stop below and target above for a long), on a bar closing at or past flat-by, and on a DLL-halted day. Check `ctx.position.qty == 0` and size the stop inside `ctx.account.drawdown_remaining` and, when it is not `None`, `daily_loss_remaining`.
- **The daily loss limit (when `dll_on`) halts the day, not the attempt:** the position is flattened, no more entries that day, and the attempt continues next session.
- **Roll window:** from the Thursday 8 days before quarterly expiry through the Tuesday of expiry week there are no bars in backtests and no entries live.
- Open positions are flattened at the account's `flat_by` (16:40 ET at the latest); do not plan to hold overnight.

## Strategy contract

`step(state, bar, ctx) -> (state, actions)` runs once per closed 1-minute bar.

- `bar`: `ts` (open time, UTC), `open`, `high`, `low`, `close`, `volume`, `contract` (unadjusted front month, e.g. `NQZ26`).
- `ctx`: `ts`; `bars` (last <= 500, ending with this bar); `position` (`qty` signed, `entry`, `stop`, `target`); `account` (`phase`, `drawdown_remaining`, `daily_loss_remaining` (`None` when the DLL is off), `today_pnl`, `target_remaining` (`None` once funded)); guard both before doing arithmetic with them; `spec` (`symbol`, `tick`, `point_value`, `minis`).
- Actions (`fenrir.core.actions`): `Bracket(side, qty, stop, target)` with absolute prices, `Flatten()`, `Cancel(order_id)`.
- Keep state in frozen dataclasses and put the params in it (`step` does not receive them).

```python
# strategies/example.py
"""At 10:30 ET, one bracket in the direction of the move since the 09:30 open."""

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from fenrir.core.actions import Bracket
from fenrir.core.sessions import NY, trading_day

MARKET = "NQ"  # price series; the contract (NQ or MNQ) is chosen per run
PARAMS = {"stop_ticks": 40, "target_ticks": 80, "qty": 1}


@dataclass(frozen=True, slots=True)
class State:
    params: dict[str, Any]
    day: date | None = None
    open_price: float | None = None


def init(params):
    return State(params=dict(params))


def step(state, bar, ctx):
    day = trading_day(bar.ts)
    if day != state.day:
        state = State(params=state.params, day=day)
    hhmm = bar.ts.astimezone(NY).strftime("%H:%M")
    if hhmm == "09:30":
        return replace(state, open_price=bar.open), []
    if hhmm != "10:30" or state.open_price is None or ctx.position.qty != 0:
        return state, []
    side, sign = ("long", 1) if bar.close > state.open_price else ("short", -1)
    p, tick = state.params, ctx.spec.tick
    stop = bar.close - sign * p["stop_ticks"] * tick
    target = bar.close + sign * p["target_ticks"] * tick
    return state, [Bracket(side, p["qty"], stop, target)]
```
