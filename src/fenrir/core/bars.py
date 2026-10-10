from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # bar OPEN time, tz-aware UTC; a bar covers [ts, ts+1min)
    open: float
    high: float
    low: float
    close: float
    volume: int
    contract: str  # front-month code the prices came from, e.g. "NQZ26"
