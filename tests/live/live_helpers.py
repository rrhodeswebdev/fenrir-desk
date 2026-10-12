"""Shared constants for live tests. Imported by name via pytest `pythonpath` (importlib mode)."""

from datetime import UTC, datetime

from backtest_helpers import NQ

from fenrir.core.instruments import ContractSpec
from fenrir.live.arming import Armed
from fenrir.live.core import Setup, Start
from fenrir.rules.schema import AccountRules

ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)


def make_setup(
    rules: AccountRules,
    *,
    mode: str = "dry",
    phase: str = "evaluation",
    start: Start | None = None,
    strategy_hash: str = "h1",
    spec: ContractSpec = NQ,
    day_start: float | None = None,
) -> Setup:
    """A runner setup (NQ by default), armed with strategy hash "h1" and an 80% safety margin."""
    armed = Armed(
        "Sim101", "strategies/orb.py", "h1", "lucid-test-50k", spec.symbol, mode, phase, ARMED_AT
    )
    return Setup(armed, strategy_hash, rules, spec, 80.0, start, day_start)
