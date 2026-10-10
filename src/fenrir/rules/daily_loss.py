"""Daily loss limit: today's realized + worst open P&L at or below -limit halts the day.

Lucid treats this as a soft breach, so it halts rather than breaches.
"""

from dataclasses import replace

from fenrir.rules.state import AccountState, Event, Mark, Rule


def daily_loss_limit(limit: float) -> Rule:
    def rule(state: AccountState, event: Event) -> AccountState:
        today = state.balance - state.day_start_balance
        if isinstance(event, Mark) and today + event.worst_open_pnl <= -limit:
            return replace(state, halted_today=True)  # ledger.book clears it at DayClosed
        return state

    return rule
