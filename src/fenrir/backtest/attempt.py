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
from fenrir.rules.account import apply, build_rules, initial_state, phase_rules, view
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
        commission=getattr(rules.costs.commission_per_side, spec.symbol),
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
    fill = Fill(
        ts=bar.ts, qty=qty, price=price, commission=env.commission * b.qty, realized_pnl=0.0
    )
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
        # a fill this bar moved the balance: the rules must check it against the floor
        quiet = sim.account.open_pnl == 0 and not (sim.fills and sim.fills[-1].ts == bar.ts)
        return sim if quiet else _event(sim, env, Mark(bar.ts, 0.0, 0.0, 0.0))
    pv = env.spec.point_value
    mark = Mark(
        bar.ts,
        worst_open_pnl(pos, bar, pv),
        best_open_pnl(pos, bar, pv),
        gross_pnl(pos.qty, pos.entry, bar.close, pv),
    )
    sim = _event(sim, env, mark)
    if isinstance(sim, AttemptResult) or not sim.account.halted_today:
        return sim
    sim = _close(sim, env, _dll_exit(sim, env, bar), bar.ts)
    # the exit's slippage and commission are realized now: check them against the floor this bar
    return sim if isinstance(sim, AttemptResult) else _event(sim, env, Mark(bar.ts, 0.0, 0.0, 0.0))


def _dll_exit(sim: _Sim, env: _Env, bar: Bar) -> float:
    """Daily loss limit hit (a day halt): exit where today's loss reaches it, filled like a stop."""
    pos, a = sim.position, sim.account
    room = phase_rules(env.rules, a.phase).daily_loss_limit + a.balance - a.day_start_balance
    level = pos.entry - room / (pos.qty * env.spec.point_value)
    price = (
        min(level, bar.open) if pos.qty > 0 else max(level, bar.open)
    )  # a gap past the level fills at the open
    return market_exit(pos.qty, price, env.spec.tick)


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
