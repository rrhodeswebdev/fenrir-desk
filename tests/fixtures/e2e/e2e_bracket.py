"""Playwright fixture strategy: one long bracket on the 09:45 ET bar each day. Pinned here, not
in strategies/, so tuning a real strategy never breaks the e2e flow; global-setup.ts copies it
into its temp strategies dir. On make_bars.py's climbing bars every trade hits its target."""

from fenrir.core.actions import Bracket
from fenrir.core.sessions import NY

MARKET = "NQ"
PARAMS = {"stop_ticks": 40, "target_ticks": 80, "qty": 2}


def init(params):
    return (params["stop_ticks"], params["target_ticks"], params["qty"])


def step(state, bar, ctx):
    local = bar.ts.astimezone(NY)
    if (local.hour, local.minute) != (9, 45) or ctx.position.qty != 0:
        return state, []
    stop_ticks, target_ticks, qty = state
    tick = ctx.spec.tick
    stop, target = bar.close - stop_ticks * tick, bar.close + target_ticks * tick
    return state, [Bracket("long", qty, stop, target)]
