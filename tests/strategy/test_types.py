from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.rules.state import AccountView
from fenrir.strategy import FLAT, Ctx, Position


def test_flat_position_has_no_prices():
    assert FLAT == Position(qty=0, entry=None, stop=None, target=None)


def test_ctx_is_frozen():
    bar = Bar(datetime(2025, 10, 6, 13, 30, tzinfo=UTC), 1.0, 2.0, 0.5, 1.5, 10, "NQZ25")
    view = AccountView(
        phase="evaluation",
        drawdown_remaining=2000.0,
        daily_loss_remaining=None,
        today_pnl=0.0,
        target_remaining=3000.0,
    )
    ctx = Ctx(
        ts=bar.ts,
        bars=(bar,),
        position=FLAT,
        account=view,
        spec=ContractSpec("NQ", "NQ", 20.0, 0.25, 1.0),
    )
    with pytest.raises(FrozenInstanceError):
        ctx.ts = bar.ts  # type: ignore[misc]
