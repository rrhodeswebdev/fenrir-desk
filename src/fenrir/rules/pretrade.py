"""Reject a bracket whose stop-out would breach a limit. Shared by backtest and live."""

from fenrir.core.actions import Bracket
from fenrir.core.instruments import ContractSpec
from fenrir.rules.account import phase_rules
from fenrir.rules.contracts import contract_limit, max_minis
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, at_or_below


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
        return (
            f"{bracket.side} stop {bracket.stop} is not on the losing side of entry {entry_price}"
        )
    if sign * (bracket.target - entry_price) <= 0:
        return (
            f"{bracket.side} target {bracket.target} is not on the winning side"
            f" of entry {entry_price}"
        )
    if state.halted_today:
        return "daily loss limit hit today: no new entries until the next trading day"
    if reason := contract_limit(max_minis(rules, state), held_minis, bracket.qty * spec.minis):
        return reason
    loss = (abs(entry_price - bracket.stop) + spec.tick) * spec.point_value * bracket.qty + (
        2 * getattr(rules.costs.commission_per_side, spec.symbol) * bracket.qty
    )
    equity_after = state.balance + state.open_pnl - loss
    if at_or_below(equity_after, state.floor):
        return (
            f"stop-out loss {loss:.2f} would put equity {equity_after:.2f}"
            f" at or below drawdown floor {state.floor:.2f}"
        )
    limit = phase_rules(rules, state.phase).daily_loss_limit
    today_after = state.balance - state.day_start_balance + state.open_pnl - loss
    if limit is not None and at_or_below(today_after, -limit):
        return (
            f"stop-out loss {loss:.2f} would put day P&L {today_after:.2f}"
            f" at or below daily loss limit -{limit:.2f}"
        )
    return None
