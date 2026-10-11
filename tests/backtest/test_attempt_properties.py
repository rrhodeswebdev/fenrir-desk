from datetime import timedelta
from pathlib import Path

from backtest_helpers import D1, NQ, et, make_rules
from hypothesis import given, settings
from hypothesis import strategies as st

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket
from fenrir.core.bars import Bar
from fenrir.core.sessions import NY, trading_day
from fenrir.strategy import Strategy

TICK = NQ.tick
MINUTE = timedelta(minutes=1)


def walk(moves: list[tuple[int, int, int]]) -> tuple[Bar, ...]:
    """Random walk from 14:00 ET, skipping the 17:00 ET hour (crosses flat-by, a day)."""
    out, ts, price = [], et(D1, "14:00"), 20000.0
    for close_ticks, up, down in moves:
        while ts.astimezone(NY).hour == 17:
            ts += MINUTE
        o, c = price, price + close_ticks * TICK
        out.append(Bar(ts, o, max(o, c) + up * TICK, min(o, c) - down * TICK, c, 1, "NQZ25"))
        price, ts = c, ts + MINUTE
    return tuple(out)


def toy(every: int, stop: int, target: int) -> Strategy:
    """Alternates long/short brackets every `every` bars while flat."""

    def step(n, bar, ctx):
        if n % every or ctx.position.qty:
            return n + 1, []
        sign = 1 if (n // every) % 2 == 0 else -1
        side = "long" if sign == 1 else "short"
        b = Bracket(side, 1, bar.close - sign * stop * TICK, bar.close + sign * target * TICK)
        return n + 1, [b]

    return Strategy("toy", Path("toy.py"), "0" * 64, "NQ", {}, lambda p: 0, step)


inputs = given(
    moves=st.lists(
        st.tuples(st.integers(-8, 8), st.integers(0, 6), st.integers(0, 6)),
        min_size=2,
        max_size=400,
    ),
    every=st.integers(1, 30),
    stop=st.integers(1, 40),
    target=st.integers(1, 40),
    drawdown=st.sampled_from([150.0, 2000.0]),
)


def run(moves, every, stop, target, drawdown):
    bars = walk(moves)
    rules = make_rules(drawdown=drawdown)
    result = run_attempt(bars, 0, len(bars), toy(every, stop, target), rules, NQ)
    return bars, result


@inputs
@settings(max_examples=200, deadline=None)
def test_fills_never_better_than_the_bar_allows(moves, every, stop, target, drawdown):
    bars, r = run(moves, every, stop, target, drawdown)
    by_ts = {b.ts: b for b in bars}
    for n, f in enumerate(r.fills):
        b = by_ts[f.ts]
        if f.qty > 0:
            assert f.price >= b.low  # never bought below the bar
        else:
            assert f.price <= b.high  # never sold above the bar
        if n % 2 == 0:  # entries: next open, 1 tick against us
            assert f.price == (b.open + TICK if f.qty > 0 else b.open - TICK)


@inputs
@settings(max_examples=200, deadline=None)
def test_no_fills_after_breach(moves, every, stop, target, drawdown):
    _, r = run(moves, every, stop, target, drawdown)
    if r.breach is not None:
        assert r.outcome == "breached"
        assert all(f.ts <= r.breach.ts for f in r.fills)


@inputs
@settings(max_examples=100, deadline=None)
def test_same_input_same_result(moves, every, stop, target, drawdown):
    args = (moves, every, stop, target, drawdown)
    assert run(*args)[1] == run(*args)[1]


@inputs
@settings(max_examples=200, deadline=None)
def test_positions_never_span_trading_days(moves, every, stop, target, drawdown):
    _, r = run(moves, every, stop, target, drawdown)
    entries, exits = r.fills[0::2], r.fills[1::2]
    for entry, exit_ in zip(entries, exits, strict=False):
        assert trading_day(entry.ts) == trading_day(exit_.ts)
        assert entry.qty == -exit_.qty
