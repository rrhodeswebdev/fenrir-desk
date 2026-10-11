from datetime import UTC, datetime

import pytest

from fenrir.backtest.fills import (
    OpenPosition,
    best_open_pnl,
    bracket_exit,
    entry_fill,
    gross_pnl,
    market_exit,
    worst_open_pnl,
)
from fenrir.core.bars import Bar

TS = datetime(2025, 10, 6, 13, 31, tzinfo=UTC)
TICK = 0.25


def b(o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(TS, o, hi, lo, c, 1, "NQZ25")


LONG = OpenPosition(qty=1, entry=100.0, stop=95.0, target=105.0, opened=TS)
SHORT = OpenPosition(qty=-1, entry=100.0, stop=105.0, target=95.0, opened=TS)


def test_entry_pays_one_tick():
    assert entry_fill("long", b(100, 101, 99, 100), TICK) == 100.25
    assert entry_fill("short", b(100, 101, 99, 100), TICK) == 99.75


def test_market_exit_pays_one_tick():
    assert market_exit(2, 100.0, TICK) == 99.75
    assert market_exit(-2, 100.0, TICK) == 100.25


@pytest.mark.parametrize(
    ("pos", "bar", "expected"),
    [
        (LONG, b(100, 104, 96, 101), None),
        (LONG, b(100, 106, 99, 105), 105.0),  # target at the limit
        (LONG, b(100, 101, 94, 95), 94.75),  # stop minus a tick
        (LONG, b(100, 106, 94, 100), 94.75),  # both inside: stop first
        (LONG, b(93, 94, 92, 93), 92.75),  # gap through stop: open minus a tick
        (SHORT, b(100, 104, 96, 101), None),
        (SHORT, b(100, 101, 94, 95), 95.0),
        (SHORT, b(100, 106, 99, 105), 105.25),
        (SHORT, b(100, 106, 94, 100), 105.25),
        (SHORT, b(107, 108, 106, 107), 107.25),
    ],
)
def test_bracket_exit(pos, bar, expected):
    assert bracket_exit(pos, bar, TICK) == expected


def test_pnl():
    assert gross_pnl(1, 100.0, 105.0, 20.0) == 100.0
    assert gross_pnl(-2, 100.0, 105.0, 20.0) == -200.0


def test_worst_open_pnl_uses_adverse_extreme():
    assert worst_open_pnl(LONG, b(100, 104, 97, 101), 20.0) == -60.0
    assert worst_open_pnl(SHORT, b(100, 103, 97, 101), 20.0) == -60.0


def test_best_open_pnl_uses_favorable_extreme():
    assert best_open_pnl(LONG, b(100, 104, 97, 101), 20.0) == 80.0
    assert best_open_pnl(SHORT, b(100, 103, 97, 101), 20.0) == 60.0
