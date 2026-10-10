"""Opening-range breakout. The first `range_min` minutes after 09:30 ET set a range; the
first bar that closes outside it enters one bracket in that direction. One trade per day."""

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from fenrir.core.actions import Bracket
from fenrir.core.sessions import NY, trading_day

MARKET = "NQ"
PARAMS = {"range_min": 15, "stop_ticks": 40, "target_ticks": 80, "qty": 1}

OPEN_MINUTE = 9 * 60 + 30


@dataclass(frozen=True, slots=True)
class State:
    params: dict[str, Any]
    day: date | None = None
    high: float | None = None
    low: float | None = None
    traded: bool = False


def init(params):
    return State(params=dict(params))


def step(state, bar, ctx):
    day = trading_day(bar.ts)
    if day != state.day:
        state = State(params=state.params, day=day)
    local = bar.ts.astimezone(NY)
    minute = local.hour * 60 + local.minute - OPEN_MINUTE
    p = state.params
    if minute < 0:
        return state, []
    if minute < p["range_min"]:
        high = bar.high if state.high is None else max(state.high, bar.high)
        low = bar.low if state.low is None else min(state.low, bar.low)
        return replace(state, high=high, low=low), []
    if state.traded or state.high is None or ctx.position.qty != 0:
        return state, []
    if bar.close > state.high:
        side, sign = "long", 1
    elif bar.close < state.low:
        side, sign = "short", -1
    else:
        return state, []
    tick = ctx.spec.tick
    stop = bar.close - sign * p["stop_ticks"] * tick
    target = bar.close + sign * p["target_ticks"] * tick
    return replace(state, traded=True), [Bracket(side, p["qty"], stop, target)]
