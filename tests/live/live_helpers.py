"""Shared constants for live tests. Imported by name via pytest `pythonpath` (importlib mode)."""

import atexit
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from backtest_helpers import NQ

from fenrir.core.bars import Bar
from fenrir.core.instruments import (
    ContractSpec,
    front_month,
    in_roll_window,
    load_instruments,
    read_instruments,
)
from fenrir.data.store import SERIES, write_bars
from fenrir.live import core
from fenrir.live.arming import Armed
from fenrir.live.core import DayStart, Setup, Start
from fenrir.rules.loader import read_account
from fenrir.rules.schema import AccountRules

ARMED_AT = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
STORED = (date(2026, 9, 1), date(2027, 2, 26))  # `stored` holds these trading days
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


_store: list[Path] = []  # built once per process, copied into each test's data dir


def stored(data_dir: Path) -> None:
    """The bar store `data_dir` with one 14:00 UTC bar per weekday in `STORED` outside the roll
    window, per series, on its front month: warm-up (#79) finds the previous trading day there."""
    if not _store:
        built = Path(tempfile.mkdtemp(prefix="fenrir-store-"))
        atexit.register(shutil.rmtree, built, True)
        instruments, day = load_instruments(), STORED[0]
        bars: dict[tuple[str, str], list[Bar]] = {}
        while day <= STORED[1]:
            if day.weekday() < 5 and not in_roll_window(day):
                for series in SERIES:
                    c = front_month(instruments, series, day)
                    price = 20000.0 if series == "NQ" else 6000.0
                    ts = datetime.combine(day, time(14), UTC)
                    bars.setdefault((series, c), []).append(
                        Bar(ts, price, price, price, price, 1, c)
                    )
            day += timedelta(days=1)
        for (series, c), rows in bars.items():
            write_bars(built, series, c, rows)
        _store.append(built)
    shutil.copytree(_store[0] / "bars", data_dir / "bars", dirs_exist_ok=True)
