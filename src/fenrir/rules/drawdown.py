"""Trailing max drawdown: end-of-day or intraday, with an optional lock."""

from dataclasses import replace
from datetime import datetime

from fenrir.rules.schema import Drawdown
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Mark, Rule, at_or_below


def max_drawdown(dd: Drawdown) -> Rule:
    def trail(state: AccountState, equity: float) -> AccountState:
        peak = max(state.peak, equity)
        level = peak if dd.locks_at is None else min(peak, dd.locks_at)
        return replace(state, peak=peak, floor=max(state.floor, level - dd.amount))

    def check(state: AccountState, equity: float, ts: datetime) -> AccountState | Breach:
        if at_or_below(equity, state.floor):
            reason = f"equity {equity:.2f} at or below drawdown floor {state.floor:.2f}"
            return Breach("max_drawdown", reason, ts)
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
