from datetime import UTC, date, datetime
from pathlib import Path

from fenrir.core.actions import Bracket
from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.core.sessions import NY
from fenrir.rules.state import AccountView
from fenrir.strategy import FLAT, Ctx, Position, load_strategy

ORB = Path(__file__).parents[2] / "strategies" / "orb.py"
NQ = ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0)
VIEW = AccountView("evaluation", 2000.0, None, 0.0, 3000.0)
DAY = date(2025, 10, 6)


def bar(hhmm: str, high: float, low: float, close: float, day: date = DAY) -> Bar:
    h, m = map(int, hhmm.split(":"))
    ts = datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(UTC)
    return Bar(ts, close, high, low, close, 100, "NQZ25")


def run(bars: list[Bar], position: Position = FLAT) -> list[list]:
    s = load_strategy(ORB)
    state = s.init(s.params)
    out = []
    for b in bars:
        state, actions = s.step(state, b, Ctx(b.ts, (b,), position, VIEW, NQ))
        out.append(actions)
    return out


def opening_range(day: date = DAY) -> list[Bar]:
    # 09:30–09:44 ET: range high 102, low 98
    return [
        bar(f"09:{30 + i}", 102.0 if i == 3 else 101.0, 98.0 if i == 7 else 99.0, 100.0, day)
        for i in range(15)
    ]


def test_loads():
    s = load_strategy(ORB)
    assert s.market == "NQ"
    assert s.params == {"range_min": 15, "stop_ticks": 40, "target_ticks": 80, "qty": 1}


def test_no_action_inside_the_range():
    assert all(a == [] for a in run(opening_range()))


def test_long_breakout_brackets_from_close():
    actions = run([*opening_range(), bar("09:45", 103.0, 101.0, 102.5)])
    assert actions[-1] == [Bracket("long", 1, 102.5 - 40 * 0.25, 102.5 + 80 * 0.25)]


def test_short_breakout():
    actions = run([*opening_range(), bar("09:45", 99.0, 97.0, 97.5)])
    assert actions[-1] == [Bracket("short", 1, 97.5 + 10.0, 97.5 - 20.0)]


def test_one_trade_per_day_and_resets_next_day():
    day2 = date(2025, 10, 7)
    bars = [
        *opening_range(),
        bar("09:45", 103.0, 101.0, 102.5),
        bar("09:46", 104.0, 102.0, 103.5),
        *opening_range(day2),
        bar("09:45", 103.0, 101.0, 102.5, day2),
    ]
    actions = run(bars)
    assert actions[15] != [] and actions[16] == []
    assert actions[-1] == [Bracket("long", 1, 92.5, 122.5)]


def test_no_entry_while_in_position():
    actions = run(
        [*opening_range(), bar("09:45", 103.0, 101.0, 102.5)],
        Position(1, 100.0, 90.0, 120.0),
    )
    assert actions[-1] == []


def test_no_trade_when_data_starts_after_the_range():
    assert run([bar("10:00", 120.0, 80.0, 119.0), bar("10:01", 130.0, 70.0, 75.0)]) == [
        [],
        [],
    ]


def test_no_entry_at_or_after_16_00_et():
    # 15:59 ET (before cash close) should allow entry
    actions = run([*opening_range(), bar("15:59", 103.0, 101.0, 102.5)])
    assert actions[-1] == [Bracket("long", 1, 102.5 - 40 * 0.25, 102.5 + 80 * 0.25)]

    # 16:00 ET (at cash close) should block entry
    actions = run([*opening_range(), bar("16:00", 103.0, 101.0, 102.5)])
    assert actions[-1] == []


def test_no_action_from_evening_bars():
    # Evening bar at 18:05 ET on DAY (belongs to next trading day) should not enter
    actions = run([*opening_range(), bar("18:05", 120.0, 95.0, 110.0)])
    assert actions[-1] == []
