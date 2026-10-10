from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

_LETTERS = {3: "H", 6: "M", 9: "U", 12: "Z"}


@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str  # "ES" | "MES" | "NQ" | "MNQ"
    series: str  # "ES" | "NQ" (price series)
    point_value: float  # $ per point
    tick: float
    minis: float  # mini-equivalents per contract: 1.0 for ES/NQ, 0.1 for MES/MNQ


@dataclass(frozen=True, slots=True)
class Roll:
    series: str
    start: date  # first trading day this contract is front month
    contract: str  # "NQZ26"


@dataclass(frozen=True, slots=True)
class Instruments:
    specs: tuple[ContractSpec, ...]
    rolls: tuple[Roll, ...]  # sorted by (series, start)


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def quarterly_rolls(series: str, first_year: int, last_year: int) -> tuple[Roll, ...]:
    """CME roll: front month moves to the next quarter 8 days before expiry (3rd Friday)."""

    def code(year: int, month: int) -> str:
        return f"{series}{_LETTERS[month]}{year % 100:02d}"

    def next_quarter(year: int, month: int) -> tuple[int, int]:
        return (year + 1, 3) if month == 12 else (year, month + 3)

    return (Roll(series, date(first_year, 1, 1), code(first_year, 3)),) + tuple(
        Roll(series, _third_friday(y, m) - timedelta(days=8), code(*next_quarter(y, m)))
        for y in range(first_year, last_year + 1)
        for m in (3, 6, 9, 12)
    )


def parse_instruments(doc: Mapping[str, Any]) -> Instruments:
    specs = tuple(
        ContractSpec(
            str(c["symbol"]),
            str(c["series"]),
            float(c["point_value"]),
            float(c["tick"]),
            float(c["minis"]),
        )
        for c in doc["contracts"]
    )
    r = doc["rollover"]
    rolls = tuple(
        sorted(
            (
                roll
                for s in r["series"]
                for roll in quarterly_rolls(s, r["first_year"], r["last_year"])
            ),
            key=lambda roll: (roll.series, roll.start),
        )
    )
    return Instruments(specs, rolls)


def load_instruments(path: Path = Path("instruments.yaml")) -> Instruments:
    return parse_instruments(yaml.safe_load(path.read_text()))


def spec(instruments: Instruments, symbol: str) -> ContractSpec:
    for s in instruments.specs:
        if s.symbol == symbol:
            return s
    raise ValueError(f"unknown contract symbol {symbol!r}")


def front_month(instruments: Instruments, series: str, day: date) -> str:
    rolls = [r for r in instruments.rolls if r.series == series]
    i = bisect_right([r.start for r in rolls], day)
    if i == 0:
        first = rolls[0].start if rolls else "none"
        raise ValueError(f"no {series} contract for {day}; calendar starts {first}")
    return rolls[i - 1].contract
