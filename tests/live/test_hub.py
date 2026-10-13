import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from live_helpers import ARMED_AT

from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.service import LiveHub, run_bars
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


def bar(ts: datetime, contract: str = "NQZ26") -> Bar:
    return Bar(ts, 20000.0, 20005.0, 19995.0, 20000.0, 10, contract)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", private_dir=tmp_path / "private")


@pytest.fixture
def conn(settings):
    c = journal.open_db(settings.private_dir / "fenrir.db")
    yield c
    c.close()


def strategy(tmp_path, source: str = ALWAYS, name: str = "always"):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    return path


@pytest.fixture
def armed(tmp_path) -> Armed:
    path = strategy(tmp_path)
    return Armed(
        "Sim101", str(path), file_hash(path), "lucid-flex-50k", "MNQ", "dry", "evaluation", ARMED_AT
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The hub's wall clock, pinned to T0 so the bars here are fresh (decide checks bar age)."""
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture
async def hub(settings):
    h = LiveHub(settings)
    yield h
    await h.stop()


def kinds(conn, account="Sim101"):
    return [r.kind for r in journal.rows(conn, account=account)]


async def test_runs_armed_dry_account(hub, conn, armed):
    arming.insert_armed(conn, armed)
    await hub.tick(T0)
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action"]
    assert hub.last_bar_ts == T0
    [acc] = hub.status()["accounts"]
    assert (acc["account"], acc["mode"], acc["halted"]) == ("Sim101", "dry", False)
    [child] = hub.children.values()
    await hub.stop()
    assert child.returncode is not None and hub.conn is None


async def test_a_late_bar_is_not_traded_at_its_old_close(hub, conn, armed, clock):
    arming.insert_armed(conn, armed)
    clock["t"] = T0 + timedelta(minutes=1, seconds=181)  # closed 181 s ago; the default is 180
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "rejection"]
    [row] = [r for r in journal.rows(conn, account="Sim101") if r.kind == "rejection"]
    assert row.payload["reason"].startswith("bar closed 181s ago (over 180s)")


async def test_bar_starts_a_newly_armed_account_without_a_tick(hub, conn, armed):
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action"]


async def test_strategy_state_carries_across_bars(hub, conn, armed, tmp_path):
    counting = ALWAYS.replace("[Bracket(", "[] if state < 2 else [Bracket(")
    path = strategy(tmp_path, counting, "counting")
    arming.insert_armed(
        conn, replace(armed, strategy_path=str(path), strategy_hash=file_hash(path))
    )
    for i in range(3):
        await hub.handle_bar(bar(T0 + timedelta(minutes=i)))
    assert kinds(conn) == ["bar", "bar", "bar", "action"]


async def test_ignores_bars_of_other_series(hub, conn, armed):
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(T0, contract="ESZ26"))
    await hub.handle_bar(bar(T0, contract="NQH27"))  # not the front month yet
    assert kinds(conn) == []


async def test_disarm_drops_account_and_its_child(hub, conn, armed):
    arming.insert_armed(conn, armed)
    await hub.tick(T0)
    [child] = hub.children.values()
    arming.delete_armed(conn, "Sim101")
    await hub.tick(T0 + timedelta(seconds=5))
    assert hub.states == {} and hub.children == {}
    assert child.returncode is not None


async def test_broken_armed_row_alerts_once(hub, conn, armed):
    arming.insert_armed(conn, replace(armed, rules_account="lucid-nope-1k"))
    await hub.tick(T0)
    await hub.tick(T0 + timedelta(seconds=5))
    assert kinds(conn) == ["alert"]
    [acc] = hub.status()["accounts"]
    assert acc["halted"] is True and acc["halt_reason"].startswith("cannot start: ")
    assert acc["balance"] is None


async def test_changed_file_is_journaled_as_halt(hub, conn, armed):
    arming.insert_armed(conn, replace(armed, strategy_hash="old"))
    await hub.tick(T0)
    assert kinds(conn) == ["halt"]
    assert hub.children == {}  # the changed file is never run


async def test_halts_survive_a_restart_until_rearmed(hub, conn, armed):
    arming.insert_armed(conn, armed)
    error = {"reason": "strategy error: boom", "kind": None, "until": None}
    breach = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}
    journal.write(conn, ARMED_AT - timedelta(minutes=1), "Sim101", "halt", error)  # earlier arming
    journal.write(conn, ARMED_AT + timedelta(minutes=1), "Sim101", "halt", breach)
    await hub.tick(T0)  # a fresh hub: the runner restarted
    [acc] = hub.status()["accounts"]
    assert (acc["halted"], acc["halt_kind"]) == (True, "breach")
    assert acc["halt_reason"] == "breach max_drawdown: x"
    await hub.handle_bar(bar(T0))
    assert kinds(conn)[-2:] == ["bar", "rejection"]
    arming.insert_armed(conn, replace(armed, armed_at=T0 + timedelta(minutes=5)))  # re-armed
    await hub.tick(T0 + timedelta(minutes=5))
    [acc] = hub.status()["accounts"]
    assert acc["halted"] is False


async def test_start_gets_the_peaks_since_arming(hub, conn, armed, monkeypatch):
    calls = []
    real = core.start

    def spy(setup, now, halts=(), peaks=()):
        calls.append(list(peaks))
        return real(setup, now, halts, peaks)

    monkeypatch.setattr(core, "start", spy)
    arming.insert_armed(conn, armed)
    mine = {"peak": 50_200.0, "ts": "2026-10-12T13:01:00+00:00"}
    journal.write(conn, ARMED_AT - timedelta(minutes=1), "Sim101", "peak", {"peak": 9e9})  # earlier
    journal.write(conn, ARMED_AT + timedelta(minutes=1), "Sim101", "peak", mine)
    journal.write(conn, ARMED_AT + timedelta(minutes=2), "Sim101", "alert", {})
    await hub.tick(T0)
    assert calls == [[mine]]


async def test_a_row_after_the_close_is_no_proof_of_a_roll(hub, conn, armed, monkeypatch, clock):
    """The hub cannot start sim yet (23.3): run the real start on a sim setup with what it read."""
    got = []
    real = core.start

    def sim_start(setup, now, *rest):
        start = core.Start(50_000.0, 50_000.0, 50_000.0)
        sim = replace(setup, armed=replace(setup.armed, mode="sim"), start=start)
        got.append(real(sim, now, *rest)[0])
        return real(setup, now, *rest)

    monkeypatch.setattr(core, "start", sim_start)
    arming.insert_armed(conn, armed)  # Monday 09:00 ET; Lucid Flex trails end_of_day
    after = datetime(2026, 10, 12, 22, 5, tzinfo=UTC)  # 18:05 ET: Tuesday's trading day
    stale = {"message": "no NQ bar for 240s; new entries paused"}  # on_tick: no bar, no roll
    journal.write(conn, after, "Sim101", "alert", stale)
    clock["t"] = after + timedelta(minutes=5)
    await hub.tick(clock["t"])
    assert got[0].halted.startswith("confirm balances: ")


async def test_file_edited_while_armed_halts_on_next_bar(hub, conn, armed):
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(T0))
    with open(armed.strategy_path, "a") as f:
        f.write("\n# tweak\n")
    await hub.handle_bar(bar(T0 + timedelta(minutes=1)))
    assert kinds(conn) == ["bar", "action", "halt", "bar", "rejection"]
    assert "changed since arming" in hub.states["Sim101"].halted


async def test_strategy_error_halts_only_that_account(hub, conn, armed, tmp_path):
    path = strategy(tmp_path, ALWAYS.replace("state + 1", "1 / 0"), "broken")
    arming.insert_armed(conn, armed)
    arming.insert_armed(
        conn,
        replace(armed, account="Sim102", strategy_path=str(path), strategy_hash=file_hash(path)),
    )
    await hub.handle_bar(bar(T0))
    await hub.handle_bar(bar(T0 + timedelta(minutes=1)))
    assert kinds(conn, "Sim102") == ["bar", "halt", "bar"]
    assert "ZeroDivisionError" in hub.states["Sim102"].halted
    assert kinds(conn) == ["bar", "action", "bar", "action"]


async def test_step_that_never_returns_halts(settings, conn, armed, tmp_path):
    path = strategy(
        tmp_path,
        ALWAYS.replace("    return state + 1", "    while True:\n        pass\n    return state"),
        "spin",
    )
    arming.insert_armed(
        conn, replace(armed, strategy_path=str(path), strategy_hash=file_hash(path))
    )
    hub = LiveHub(settings, step_timeout=1)
    try:
        await hub.handle_bar(bar(T0))
        assert hub.children == {}  # killed, so it cannot spin on
    finally:
        await hub.stop()
    assert kinds(conn) == ["bar", "halt"]
    assert "over 1 s" in hub.states["Sim101"].halted


async def test_nothing_armed_opens_no_database(hub, settings):
    await hub.tick(T0)
    await hub.handle_bar(bar(T0))
    assert not (settings.private_dir / "fenrir.db").exists()


async def test_run_bars_feeds_the_hub_and_connects_on_the_first_bar(hub, conn, armed):
    arming.insert_armed(conn, armed)
    subscribed, go, fed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fake_stream(settings, contracts):
        assert {c[:2] for c in contracts} == {"ES", "NQ"}
        subscribed.set()
        await go.wait()
        yield bar(T0)
        fed.set()  # resumed only once the hub has handled the bar
        await asyncio.sleep(3600)

    task = asyncio.create_task(run_bars(hub, fake_stream))
    try:
        await asyncio.wait_for(subscribed.wait(), 120)
        assert hub.lse_connected is False  # subscribed is not streaming
        go.set()
        await asyncio.wait_for(fed.wait(), 120)
        assert hub.lse_connected is True
        assert kinds(conn) == ["bar", "action"]
    finally:
        task.cancel()


async def test_an_overflowing_number_halts_only_that_account(hub, conn, armed, tmp_path):
    huge = strategy(tmp_path, ALWAYS.replace('"long", 1,', '"long", 10**400,'), "huge")
    arming.insert_armed(conn, armed)
    arming.insert_armed(
        conn,
        replace(armed, account="Sim102", strategy_path=str(huge), strategy_hash=file_hash(huge)),
    )
    await hub.handle_bar(bar(T0))
    assert kinds(conn, "Sim102") == ["bar", "halt"]
    assert "not an action" in hub.states["Sim102"].halted
    assert kinds(conn) == ["bar", "action"]


async def test_an_unexpected_error_in_one_account_spares_the_others(hub, conn, armed, monkeypatch):
    on_bar = core.on_bar

    def failing(s, b):
        if s.setup.armed.account == "Sim101":
            raise OverflowError("boom")
        return on_bar(s, b)

    monkeypatch.setattr(core, "on_bar", failing)
    arming.insert_armed(conn, armed)  # Sim101 runs first: armed rows are in account order
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["halt"]
    assert hub.states["Sim101"].halted == "live runner error: OverflowError: boom"
    assert "Sim101" not in hub.children
    assert kinds(conn, "Sim102") == ["bar", "action"]


async def test_a_lost_halt_row_still_halts_and_spares_the_others(hub, conn, armed, monkeypatch):
    """The journal itself fails (sqlite locked past its busy timeout) for one account's halt."""
    on_bar, write = core.on_bar, journal.write

    def failing(s, b):
        if s.setup.armed.account == "Sim101":
            raise OverflowError("boom")
        return on_bar(s, b)

    def locked(conn, ts, account, kind, payload):
        if kind == "halt":
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    monkeypatch.setattr(core, "on_bar", failing)
    monkeypatch.setattr(journal, "write", locked)
    arming.insert_armed(conn, armed)
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    await hub.handle_bar(bar(T0))
    assert hub.states["Sim101"].halted == "live runner error: OverflowError: boom"
    assert kinds(conn, "Sim102") == ["bar", "action"]


BREACH = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}


def breaching(monkeypatch):
    """core.on_bar breaches on every bar, as a Mark below the floor would."""
    on_bar = core.on_bar

    def fake(s, b):
        s, out = on_bar(s, b)
        s = replace(s, halted=BREACH["reason"], halt_kind="breach")
        return s, (*out, core.Log("halt", BREACH))

    monkeypatch.setattr(core, "on_bar", fake)


async def test_a_breach_whose_row_fails_keeps_its_kind(hub, conn, armed, monkeypatch):
    """The catch-all never swaps an in-memory breach for a weaker `live runner error` halt."""
    write = journal.write

    def locked(conn, ts, account, kind, payload):
        if kind == "halt":
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    breaching(monkeypatch)
    monkeypatch.setattr(journal, "write", locked)
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(T0))
    s = hub.states["Sim101"]
    assert (s.halted, s.halt_kind) == (BREACH["reason"], "breach")


async def test_an_unjournalled_breach_survives_a_restart(hub, settings, conn, armed, monkeypatch):
    """sqlite refuses every write on the breaching bar; the halt row lands on the next tick."""
    write, locked = journal.write, {"on": True}

    def flaky(conn, ts, account, kind, payload):
        if locked["on"]:
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    arming.insert_armed(conn, armed)
    await hub.tick(T0)
    breaching(monkeypatch)
    monkeypatch.setattr(journal, "write", flaky)
    await hub.handle_bar(bar(T0))
    locked["on"] = False
    await hub.tick(T0)
    await hub.stop()
    restarted = LiveHub(settings)
    try:
        await restarted.tick(T0)
        [acc] = restarted.status()["accounts"]
    finally:
        await restarted.stop()
    assert (acc["halt_kind"], acc["halt_reason"]) == ("breach", BREACH["reason"])


async def test_disarming_drops_unjournalled_halts(hub, conn, armed, monkeypatch):
    write = journal.write

    def locked(conn, ts, account, kind, payload):
        if kind == "halt":
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    breaching(monkeypatch)
    monkeypatch.setattr(journal, "write", locked)
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(T0))
    monkeypatch.setattr(journal, "write", write)
    arming.insert_armed(conn, replace(armed, armed_at=T0 + timedelta(minutes=5)))  # re-armed
    await hub.tick(T0 + timedelta(minutes=5))
    assert "halt" not in kinds(conn)


async def test_isolation_refusal_halts_without_a_child(hub, conn, armed, monkeypatch):
    monkeypatch.setenv("FENRIR_BIND_ALL", "1")
    monkeypatch.delenv("FENRIR_PTY_USER", raising=False)
    arming.insert_armed(conn, armed)
    await hub.tick(T0)
    assert kinds(conn) == ["halt"]
    reason = hub.states["Sim101"].halted
    assert reason.startswith("cannot load the strategy: refusing to run strategy code")
    assert "FENRIR_PTY_USER is unset" in reason  # the shared isolation check's reasons
    assert hub.children == {}
