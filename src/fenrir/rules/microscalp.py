"""Possible microscalps: 1-minute bars can't show hold time, so same-bar winners are flagged."""

from dataclasses import replace

from fenrir.rules.state import AccountState, Event, TradeClosed


def microscalp(state: AccountState, event: Event) -> AccountState:
    if isinstance(event, TradeClosed) and event.same_bar and event.pnl > 0:
        return replace(state, possible_microscalp_pnl=state.possible_microscalp_pnl + event.pnl)
    return state
