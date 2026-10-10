from dataclasses import dataclass
from typing import Literal

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class Bracket:
    side: Side
    qty: int  # contracts, > 0
    stop: float  # absolute price
    target: float  # absolute price


@dataclass(frozen=True, slots=True)
class Flatten: ...


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: str


Action = Bracket | Flatten | Cancel
