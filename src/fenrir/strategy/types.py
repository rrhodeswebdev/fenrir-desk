"""Values a strategy's `step` receives. Pure data."""

from dataclasses import dataclass
from datetime import datetime

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.rules.state import AccountView


@dataclass(frozen=True, slots=True)
class Position:
    qty: int  # signed contracts; 0 = flat
    entry: float | None
    stop: float | None
    target: float | None


FLAT = Position(qty=0, entry=None, stop=None, target=None)


@dataclass(frozen=True, slots=True)
class Ctx:
    ts: datetime  # current bar's open time
    bars: tuple[Bar, ...]  # last <= 500 bars, ending with the current bar
    position: Position
    account: AccountView
    spec: ContractSpec  # the contract being traded (tick, point_value)
