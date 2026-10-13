"""Shared constants for live tests. Imported by name via pytest `pythonpath` (importlib mode)."""

from datetime import UTC, datetime

from backtest_helpers import NQ

from fenrir.core.instruments import ContractSpec
from fenrir.live import core
from fenrir.live.arming import Armed
from fenrir.live.core import DayStart, Setup, Start
from fenrir.rules.schema import AccountRules

ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
INSTRUMENT = "NQ 12-25"  # NinjaTrader's name for the tests' NQ front month


def make_setup(
    rules: AccountRules,
    *,
    mode: str = "dry",
    phase: str = "evaluation",
    start: Start | None = None,
    strategy_hash: str = "h1",
    spec: ContractSpec = NQ,
    day_start: DayStart | None = None,
) -> Setup:
    """A runner setup (NQ by default), armed with strategy hash "h1" and an 80% safety margin."""
    armed = Armed(
        "Sim101", "strategies/orb.py", "h1", "lucid-test-50k", spec.symbol, mode, phase, ARMED_AT
    )
    return Setup(armed, strategy_hash, rules, spec, 80.0, start, day_start)


def decide_now(s, actions, stale_seconds: int = 90):
    """`core.decide` at the current bar's close: the bar is fresh."""
    return core.decide(s, actions, s.last_seen, stale_seconds)


def fill(qty, price, ts, commission=0.0, cid="other"):
    """An add-on "fill" stream message (validated by fenrir.bridge.client)."""
    stamp = ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "type": "fill",
        "account": "Sim101",
        "client_id": cid,
        "order_id": "o",
        "instrument": INSTRUMENT,
        "qty": qty,
        "price": price,
        "commission": commission,
        "ts": stamp,
    }


def reconciled(s):
    """A sim or live runner that NinjaTrader's book matches (flat, no orders): entries open."""
    return core.on_reconcile(s, INSTRUMENT, (), (), s.last_seen)[0]
