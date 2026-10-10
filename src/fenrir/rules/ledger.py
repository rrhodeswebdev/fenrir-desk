"""Accounting for every event. Runs before the rules so they see post-event balances."""

from dataclasses import replace
from typing import assert_never

from fenrir.core.sessions import trading_day
from fenrir.rules.state import AccountState, DayClosed, Event, Fill, Mark, TradeClosed


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
            pnl = state.balance - state.day_start_balance
            today = ((event.day, pnl),) if state.traded_today else ()
            return replace(
                state,
                closed_days=state.closed_days + today,
                day_start_balance=state.balance,
                traded_today=False,
                halted_today=False,
            )
        case TradeClosed():
            return state
        case _:
            assert_never(event)
