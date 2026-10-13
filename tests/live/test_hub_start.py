"""#211: the hub starts a sim or live account from NinjaTrader's first account message for it
(Start.balance), with the armed phase start and peak (DEC-4a) and today's journalled open;
until that message the account waits, sends nothing and its status says why."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from live_helpers import ARMED_AT, pinned

from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.core import DayStart, Start
from fenrir.live.service import LiveHub
from fenrir.settings import Settings
from fenrir.strategy import file_hash

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26

ALWAYS = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    return state + 1, [Bracket("long", 1, bar.close - 10, bar.close + 20)]
"""


def bar(ts: datetime) -> Bar:
    return Bar(ts, 20000.0, 20005.0, 19995.0, 20000.0, 10, "NQZ26")


def account(
    balance: float, account_id: str = "Sim101", is_sim: bool = True, realized: float = 0.0
) -> dict:
    """An add-on "account" stream message, absolute $."""
    return {
        "type": "account",
        "id": account_id,
        "name": account_id,
        "is_sim": is_sim,
        "balance": balance,
        "realized_pnl_today": realized,
        "unrealized_pnl": 0.0,
    }


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")


@pytest.fixture
def conn(settings):
    c = journal.open_db(settings.private_dir / "fenrir.db")
    yield c
    c.close()


@pytest.fixture
def armed(tmp_path, conn) -> Armed:
    """Sim101 armed in sim, Lucid Flex 50k (end_of_day), phase start $50,000, peak $50,500."""
    path = tmp_path / "always.py"
    path.write_text(ALWAYS)
    a = pinned(
        Armed(
            "Sim101",
            str(path),
            file_hash(path),
            "lucid-flex-50k",
            "MNQ",
            "sim",
            "evaluation",
            ARMED_AT,
            start_balance=50_000.0,
            peak_balance=50_500.0,
        )
    )
    arming.insert_armed(conn, a)
    return a


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture
async def fake():
    f = FakeAddon()
    async with running(f) as url:
        f.url = url
        yield f


@pytest.fixture
async def client(fake):
    c = AddonClient(fake.url, fake.token, timeout=0.2, retries=2)
    yield c
    await c.aclose()


@pytest.fixture
async def hub(settings, client):
    h = LiveHub(settings, addon=client)
    yield h
    await h.stop()


def kinds(conn, account="Sim101"):
    return [r.kind for r in journal.rows(conn, account=account)]


async def test_without_an_account_message_it_waits_and_sends_nothing(hub, conn, armed, fake):
    await hub.handle_stream({"type": "connected"})
    await hub.tick(T0)
    await hub.handle_bar(bar(T0))
    await hub.tick(T0 + timedelta(seconds=5))
    await hub.handle_stream(account(50_300.0, "Sim102"))  # another account's message
    assert hub.states == {} and hub.children == {}
    assert kinds(conn) == [] and fake.requests == []
    [acc] = hub.status()["accounts"]
    assert acc["halt_reason"].startswith("waiting for NinjaTrader: no account message with a")
    assert "for Sim101 yet" in acc["halt_reason"]
    assert (acc["halted"], acc["halt_kind"], acc["mode"]) == (True, None, "sim")
    assert [acc[k] for k in ("balance", "nt_balance", "limits", "position", "session")] == [
        None
    ] * 5
    assert acc["conditions"] == [{"kind": None, "reason": acc["halt_reason"]}]  # #212


async def test_the_first_account_message_starts_it_from_the_pins_and_ninjatrader(
    hub, conn, armed, fake
):
    """phase_start = armed.start_balance, peak = max(armed.peak_balance, journalled peaks),
    balance = NinjaTrader's now; no open journalled today, so that balance opens the day."""
    peak = {"peak": 50_800.0, "ts": (ARMED_AT + timedelta(minutes=1)).isoformat()}
    journal.write(conn, ARMED_AT + timedelta(minutes=1), "Sim101", "peak", peak)
    await hub.handle_stream({"type": "connected"})
    await hub.tick(T0)
    await hub.handle_stream(account(50_300.0))
    s = hub.states["Sim101"]
    assert s.setup.start == Start(50_000.0, 50_500.0, balance=50_300.0, realized_today=0.0)
    assert (s.acct.balance, s.acct.peak) == (300.0, 800.0)
    assert s.setup.day_start == DayStart(50_300.0, 50_800.0)
    assert s.nt == core.NtAccount(50_300.0, 0.0, 0.0)
    assert kinds(conn) == ["peak", "alert", "day_start"]
    assert "Sim101" in hub.children
    await hub.tick(T0 + timedelta(seconds=5))  # reconciles with NinjaTrader's (flat) book
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/bracket"]
    [acc] = hub.status()["accounts"]
    assert (acc["halted"], acc["balance"], acc["nt_balance"]) == (False, 300.0, 50_300.0)


async def test_a_restart_mid_day_opens_the_day_as_journalled_not_at_the_balance_now(
    settings, client, conn, armed, clock
):
    first = LiveHub(settings, addon=client)
    try:
        await first.tick(T0)
        await first.handle_stream(account(50_300.0))
        assert first.states["Sim101"].session_start == 300.0
    finally:
        await first.stop()
    clock["t"] = T0 + timedelta(hours=2)
    second = LiveHub(settings, addon=client)
    try:
        await second.tick(clock["t"])
        await second.handle_stream(account(49_900.0, realized=-400.0))  # down $400 today
        s = second.states["Sim101"]
        assert s.setup.day_start == DayStart(50_300.0, 50_500.0)
        assert (s.acct.balance, s.acct.day_start_balance, s.session_start) == (-100.0, 300.0, 300.0)
        assert second.status()["accounts"][0]["today_pnl"] == -400.0
        assert kinds(conn).count("day_start") == 1  # the restart opened nothing new
    finally:
        await second.stop()


async def test_a_mode_mismatch_in_the_first_message_halts(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_stream(account(50_300.0, is_sim=False))
    s = hub.states["Sim101"]
    assert s.halted == "mode mismatch: armed sim, NinjaTrader reports a live account"


async def test_a_row_disarmed_while_waiting_never_starts(hub, conn, armed):
    await hub.tick(T0)
    arming.delete_armed(conn, "Sim101")
    await hub.tick(T0 + timedelta(seconds=5))
    await hub.handle_stream(account(50_300.0))
    assert hub.states == {} and hub.status()["accounts"] == []
    assert kinds(conn) == []


async def test_a_sim_row_without_its_balances_halts_at_once(hub, conn, armed):
    arming.insert_armed(conn, replace(armed, peak_balance=None))
    await hub.tick(T0)
    s = hub.states["Sim101"]
    assert not s.started and "DEC-4a" in s.halted
    assert kinds(conn) == ["halt"]


async def test_a_strategy_that_cannot_load_still_journals_the_open_and_the_mismatch(
    hub, conn, armed, monkeypatch
):
    async def refused(a):
        raise ValueError("isolation refused")

    monkeypatch.setattr(hub, "_spawn", refused)
    await hub.tick(T0)
    await hub.handle_stream(account(50_300.0, is_sim=False))
    assert kinds(conn) == ["alert", "day_start", "halt"]
    assert hub.states["Sim101"].halted.startswith("mode mismatch: ")  # the first halt stands


@pytest.mark.parametrize("loss", [0.0, 800.0, 1000.0])
async def test_a_morning_loss_before_the_first_start_counts_once(hub, conn, armed, fake, loss):
    """No open journalled: the day opens at NinjaTrader's balance less its realized P&L today,
    so the room is the real one and no false safety halt stops the account (review of #217)."""
    arming.insert_armed(conn, replace(armed, peak_balance=50_000.0))  # floor 48,000
    await hub.handle_stream({"type": "connected"})
    await hub.tick(T0)
    await hub.handle_stream(account(50_000.0 - loss, realized=-loss))
    await hub.tick(T0 + timedelta(seconds=5))
    await hub.handle_bar(bar(T0))
    [acc] = hub.status()["accounts"]
    assert (acc["today_pnl"], acc["limits"]["drawdown_remaining"]) == (-loss, 2000.0 - loss)
    assert acc["halted"] is False and [path for path, _ in fake.requests] == ["/bracket"]


async def test_a_balance_at_or_below_zero_is_not_a_figure_yet(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_stream(account(0.0))  # e.g. NinjaTrader before its account data loads
    await hub.handle_stream(account(-5.0))
    assert hub.states == {} and "Sim101" in hub.waiting and kinds(conn) == []
    await hub.handle_stream(account(50_300.0))
    assert hub.states["Sim101"].halted is None


async def test_a_first_balance_at_the_liquidation_level_cannot_start(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_stream(account(45_000.0))  # the pins' level: 50,000 + 500 - 2,000
    s = hub.states["Sim101"]
    assert s.halted.startswith("confirm balances: NinjaTrader's balance 45000.00 is at or below")
    assert not s.started and s.halt_kind is None and hub.children == {}
    assert kinds(conn) == ["halt"]


async def test_a_message_after_a_rearm_starts_the_new_arming_not_the_old(
    hub, conn, armed, fake, clock
):
    """The stream syncs `armed` before starting a waiting row (review of #217): a disarm and a
    re-arm with a corrected peak since the last tick never start the old arming."""
    await hub.tick(T0)
    clock["t"] = T0 + timedelta(seconds=1)
    arming.disarm(conn, "Sim101", clock["t"])
    new = replace(armed, armed_at=T0 + timedelta(seconds=2), peak_balance=50_300.0)
    arming.insert_armed(conn, new)
    clock["t"] = T0 + timedelta(seconds=3)
    await hub.handle_stream(account(50_300.0))  # before the next tick's sync
    s = hub.states["Sim101"]
    assert s.setup.armed == new and s.setup.day_start == DayStart(50_300.0, 50_300.0)
    assert (s.acct.floor, s.session_floor) == (-1700.0, -1700.0)
    rows = journal.rows(conn, account="Sim101")
    assert [r.kind for r in rows] == ["halt", "alert", "day_start"]  # disarmed, then the new start
    assert rows[2].payload["peak"] == 50_300.0


async def test_a_waiting_row_the_kill_could_not_disarm_never_loads_its_strategy(
    hub, conn, armed, fake, monkeypatch
):
    await hub.tick(T0)

    def locked(conn, account):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service, "delete_armed", locked)
    assert (await hub.kill())["ok"] is False
    [acc] = hub.status()["accounts"]
    assert acc["halt_reason"].startswith("kill switch: disarm failed for Sim101: ")  # not "waiting"
    assert acc["conditions"] == [{"kind": None, "reason": acc["halt_reason"]}]
    await hub.handle_stream(account(50_300.0))
    assert hub.states["Sim101"].halted and hub.children == {}
    await hub.tick(T0 + timedelta(seconds=5))
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/flatten-all", "/flatten-all"]
