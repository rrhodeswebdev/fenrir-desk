"""Flat-by: no position may be held at or after the session's flat-by time.

A breach means the engine failed to flatten before the cutoff (engines flatten at 16:40);
it is an engine-bug signal, not a Lucid failure (Lucid auto-closes at 16:45 and holding past
it does not fail the account).
"""

from fenrir.core.sessions import at_or_after
from fenrir.rules.state import AccountState, Breach, DayClosed, Event, Fill, Mark, Rule


def flat_by(hhmm: str, tz: str) -> Rule:
    def rule(state: AccountState, event: Event) -> AccountState | Breach:
        if state.position == 0:
            return state
        match event:
            case Fill() | Mark() if at_or_after(event.ts, hhmm, tz):
                msg = f"position {state.position} held at or after {hhmm} {tz}"
                return Breach("flat_by", msg, event.ts)
            case DayClosed():
                msg = f"position {state.position} still open at day close"
                return Breach("flat_by", msg, event.ts)
        return state

    return rule
