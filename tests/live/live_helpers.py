"""Shared constants for live tests. Imported by name via pytest `pythonpath` (importlib mode)."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from backtest_helpers import NQ

from fenrir.core.instruments import ContractSpec, read_instruments
from fenrir.live import core
from fenrir.live.arming import Armed
from fenrir.live.core import DayStart, Setup, Start
from fenrir.rules.loader import read_account
from fenrir.rules.schema import AccountRules

ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
RULES = Path(__file__).parents[2] / "rules"
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
    rules_hash: str = "r1",
    instruments_hash: str = "i1",
    dll_on: bool = False,
) -> Setup:
    """A runner setup (NQ by default), armed with strategy hash "h1", rules hash "r1",
    instruments hash "i1" and DLL off, and an 80% safety margin; the keyword hashes and
    `dll_on` are the files' as they are now (a mismatch halts at start, DEC-4d)."""
    armed = Armed(
        "Sim101",
        "strategies/orb.py",
        "h1",
        "lucid-test-50k",
        spec.symbol,
        mode,
        phase,
        ARMED_AT,
        dll_on=False,
        rules_hash="r1",
        instruments_hash="i1",
    )
    return Setup(
        armed,
        strategy_hash,
        rules,
        spec,
        80.0,
        rules_hash=rules_hash,
        instruments_hash=instruments_hash,
        dll_on=dll_on,
        start=start,
        day_start=day_start,
    )


def pinned(a: Armed, rules_dir: Path = RULES, dll_on: bool = False) -> Armed:
    """`a` with what arming pins now (DEC-4d): the rules file's and instruments.yaml's hashes."""
    _, rules_hash = read_account(a.rules_account, rules_dir)
    _, instruments_hash = read_instruments()
    return replace(a, dll_on=dll_on, rules_hash=rules_hash, instruments_hash=instruments_hash)


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
