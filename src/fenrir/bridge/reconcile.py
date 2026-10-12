"""Compare the runner's expected book with NinjaTrader's positions and working orders. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MismatchKind = Literal[
    "position", "unexpected_position", "missing_stop", "missing_target", "stray_orders"
]


@dataclass(frozen=True, slots=True)
class NtPosition:
    account: str
    instrument: str  # NinjaTrader name, e.g. "NQ 12-26"
    qty: int  # signed
    avg_price: float


@dataclass(frozen=True, slots=True)
class NtOrder:
    account: str
    order_id: str
    client_id: str
    instrument: str
    role: Literal["entry", "stop", "target", "other"]
    action: Literal["buy", "sell"]
    qty: int
    price: float
    state: str


@dataclass(frozen=True, slots=True)
class ExpectedBook:
    account: str
    instrument: str
    qty: int  # signed qty the runner believes it holds


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: MismatchKind
    detail: str


def reconcile(
    expected: ExpectedBook, positions: Sequence[NtPosition], orders: Sequence[NtOrder]
) -> tuple[Mismatch, ...]:
    """Every way NinjaTrader's book differs from `expected`; `()` when it matches.
    `orders` are working orders; rows for other accounts are ignored."""
    mine = [p for p in positions if p.account == expected.account and p.qty != 0]
    working = [o for o in orders if o.account == expected.account]
    out = [
        Mismatch("unexpected_position", f"{p.instrument} qty {p.qty}")
        for p in mine
        if p.instrument != expected.instrument
    ]
    held = sum(p.qty for p in mine if p.instrument == expected.instrument)
    if held != expected.qty:
        out.append(
            Mismatch(
                "position",
                f"runner expects {expected.qty} {expected.instrument}, NinjaTrader has {held}",
            )
        )
    closing = "sell" if held > 0 else "buy"
    # Only a stop or target that closes the held position belongs; anything else (a stale entry,
    # an "other" order, a wrong-direction stop) could add to it.
    protective = [
        o
        for o in working
        if held
        and o.instrument == expected.instrument
        and o.role in ("stop", "target")
        and o.action == closing
    ]
    if held:
        for role, kind in (("stop", "missing_stop"), ("target", "missing_target")):
            covered = sum(o.qty for o in protective if o.role == role)
            if covered != abs(held):
                out.append(
                    Mismatch(kind, f"{role} orders cover {covered} of {abs(held)} contracts")
                )
    stray = [o for o in working if o not in protective]
    if stray:
        state = f"holding {held} {expected.instrument}" if held else "flat"
        out.append(
            Mismatch(
                "stray_orders",
                f"working orders while {state}: " + ", ".join(o.order_id for o in stray),
            )
        )
    return tuple(out)
