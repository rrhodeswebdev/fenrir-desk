import asyncio
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from live_helpers import ARMED_AT, RULES, pinned

from fenrir.core.bars import Bar
from fenrir.data.live import FeedStopped
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
    return pinned(
        Armed(
            "Sim101",
            str(path),
            file_hash(path),
            "lucid-flex-50k",
            "MNQ",
            "dry",
            "evaluation",
            ARMED_AT,
        )
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


async def test_roll_window_bars_never_reach_step(hub, conn, armed, clock):
    roll = datetime(2026, 12, 15, 14, 0, tzinfo=UTC)  # in NQ Z26 -> H27's roll window
    clock["t"] = roll
    arming.insert_armed(conn, armed)
    await hub.handle_bar(bar(roll, contract="NQH27"))
    assert kinds(conn) == ["bar"]  # ALWAYS's Bracket is never asked for


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
    assert acc["conditions"] == [{"kind": None, "reason": acc["halt_reason"]}]  # a Halt cell
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


async def test_start_gets_the_peaks_and_opens_since_arming(hub, conn, armed, monkeypatch):
    calls = []
    real = core.start

    def spy(setup, now, halts=(), peaks=(), opens=()):
        calls.append((list(peaks), list(opens)))
        return real(setup, now, halts, peaks, opens)

    monkeypatch.setattr(core, "start", spy)
    arming.insert_armed(conn, armed)
    mine = {"peak": 50_200.0, "ts": "2026-10-12T13:01:00+00:00"}
    day = {"day": "2026-10-12", "balance": 50_100.0, "peak": 50_200.0, "ts": mine["ts"]}
    journal.write(conn, ARMED_AT - timedelta(minutes=1), "Sim101", "peak", {"peak": 9e9})  # earlier
    journal.write(conn, ARMED_AT - timedelta(minutes=1), "Sim101", "day_start", {"day": "x"})
    journal.write(conn, ARMED_AT + timedelta(minutes=1), "Sim101", "peak", mine)
    journal.write(conn, ARMED_AT + timedelta(minutes=1), "Sim101", "day_start", day)
    journal.write(conn, ARMED_AT + timedelta(minutes=2), "Sim101", "alert", {})
    await hub.tick(T0)
    assert calls == [([mine], [day])]


async def test_a_row_after_the_close_is_no_proof_of_a_roll(hub, conn, armed, monkeypatch, clock):
    """Run the real start on a sim setup with what the hub read for this (dry) row."""
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


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (None, None),
        ("dll_on", "dll_on changed since arming (armed off, accounts.yaml now on); re-arm"),
        ("rules", "rules file changed since arming; re-arm"),
        ("instruments", "instruments.yaml changed since arming; re-arm"),
    ],
)
async def test_a_start_with_inputs_other_than_the_pinned_halts(
    settings, conn, armed, tmp_path, monkeypatch, change, reason
):
    """#91, DEC-4d: after a Settings change and a restart the account halts, never trading an
    untested DLL choice, rules file or instruments.yaml; its strategy never runs."""
    rules = tmp_path / "rules"
    shutil.copytree(RULES / "lucid", rules / "lucid")
    shutil.copy("instruments.yaml", tmp_path / "instruments.yaml")
    monkeypatch.chdir(tmp_path)  # the hub reads ./instruments.yaml
    edited = {
        "dll_on": settings.data_dir / "accounts.yaml",
        "rules": rules / "lucid" / "flex-50k.yaml",
        "instruments": tmp_path / "instruments.yaml",
    }
    if change == "dll_on":
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        edited[change].write_text("lucid-flex-50k: {dll_on: true}\n")
    elif change is not None:
        with edited[change].open("a") as f:
            f.write("\n# edited after arming\n")
    arming.insert_armed(conn, armed)
    hub = LiveHub(replace(settings, rules_dir=rules))
    try:
        await hub.tick(T0)
        [acc] = hub.status()["accounts"]
        assert acc["halt_reason"] == reason
        if reason is None:
            assert hub.children and kinds(conn) == []
        else:
            assert acc["halted"] is True and acc["balance"] is None
            assert hub.children == {}  # the strategy never runs
            [row] = journal.rows(conn, account="Sim101")
            assert (row.kind, row.payload["reason"]) == ("halt", reason)
    finally:
        await hub.stop()


@pytest.fixture
def own_inputs(settings, tmp_path, monkeypatch):
    """A rules dir and ./instruments.yaml the test may edit; the hub's settings use them."""
    rules = tmp_path / "rules"
    shutil.copytree(RULES / "lucid", rules / "lucid")
    shutil.copy("instruments.yaml", tmp_path / "instruments.yaml")
    monkeypatch.chdir(tmp_path)  # the hub reads ./instruments.yaml
    return replace(settings, rules_dir=rules)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("dll_on", "dll_on changed since arming (armed off, accounts.yaml now on); re-arm"),
        ("rules", "rules file changed since arming; re-arm"),
        ("instruments", "instruments.yaml changed since arming; re-arm"),
    ],
)
async def test_a_pinned_input_edited_while_armed_halts_on_the_next_bar(
    own_inputs, conn, armed, tmp_path, change, reason
):
    """SPEC DEC-4d and the failure table: halted while armed, like a changed strategy file."""
    arming.insert_armed(conn, pinned(armed, rules_dir=own_inputs.rules_dir))
    hub = LiveHub(own_inputs)
    try:
        await hub.handle_bar(bar(T0))
        if change == "dll_on":
            own_inputs.data_dir.mkdir(parents=True, exist_ok=True)
            (own_inputs.data_dir / "accounts.yaml").write_text("lucid-flex-50k: {dll_on: true}\n")
        else:
            edited = {
                "rules": own_inputs.rules_dir / "lucid" / "flex-50k.yaml",
                "instruments": tmp_path / "instruments.yaml",
            }[change]
            with edited.open("a") as f:
                f.write("\n# edited while armed\n")
        await hub.handle_bar(bar(T0 + timedelta(minutes=1)))
        assert kinds(conn) == ["bar", "action", "halt", "bar", "rejection"]
        assert hub.states["Sim101"].halted == reason
    finally:
        await hub.stop()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("dll_on", "dll_on changed since arming (armed off, accounts.yaml now on); re-arm"),
        ("rules", "rules file changed since arming; re-arm"),
        ("instruments", "instruments.yaml changed since arming; re-arm"),
        ("strategy", "strategy file changed since arming; re-arm"),
        ("broken", "live runner error: "),
    ],
)
async def test_a_pinned_input_edited_while_no_bars_arrive_halts_on_the_next_tick(
    own_inputs, conn, armed, tmp_path, change, reason
):
    """#211 (folded from #208): the 5 s tick checks the pins too, so an edit halts the account
    while the bar feed is down; a broken accounts.yaml halts it as on a bar."""
    arming.insert_armed(conn, pinned(armed, rules_dir=own_inputs.rules_dir))
    hub = LiveHub(own_inputs)
    try:
        await hub.tick(T0)
        own_inputs.data_dir.mkdir(parents=True, exist_ok=True)
        edited = {
            "dll_on": (own_inputs.data_dir / "accounts.yaml", "lucid-flex-50k: {dll_on: true}\n"),
            "broken": (own_inputs.data_dir / "accounts.yaml", "lucid-flex-50k: [\n"),
            "rules": (own_inputs.rules_dir / "lucid" / "flex-50k.yaml", None),
            "instruments": (tmp_path / "instruments.yaml", None),
            "strategy": (Path(armed.strategy_path), None),
        }
        path, text = edited[change]
        if text is None:
            with path.open("a") as f:
                f.write("\n# edited while armed\n")
        else:
            path.write_text(text)
        await hub.tick(T0 + timedelta(seconds=5))
        assert hub.states["Sim101"].halted.startswith(reason)
        assert kinds(conn) == ["halt"]
        await hub.tick(T0 + timedelta(seconds=10))
        assert kinds(conn) == ["halt"]  # once
    finally:
        await hub.stop()


async def test_a_halted_account_whose_front_month_fails_logs_it_once(
    own_inputs, conn, armed, tmp_path, caplog
):
    """#211 (folded from #208): the halt is journalled once, and so is the error logged."""
    p = tmp_path / "instruments.yaml"
    p.write_text(p.read_text().replace("last_year: 2030", "last_year: 2025"))
    arming.insert_armed(conn, pinned(armed, rules_dir=own_inputs.rules_dir))
    hub = LiveHub(own_inputs)
    try:
        for minute in range(3):
            await hub.handle_bar(bar(T0 + timedelta(minutes=minute)))
        assert hub.states["Sim101"].halted.startswith("live runner error: ValueError: no NQ")
        assert kinds(conn) == ["halt"]
        failed = [r for r in caplog.records if "failed on a bar" in r.getMessage()]
        assert len(failed) == 1 and failed[0].exc_info is not None
    finally:
        await hub.stop()


async def test_each_account_keeps_the_instruments_it_started_with(
    own_inputs, conn, armed, tmp_path
):
    """Another account's start never swaps a running account's instruments; an account whose
    bar cannot be checked (here: its calendar ends before today) halts instead of going dark."""
    arming.insert_armed(conn, pinned(armed, rules_dir=own_inputs.rules_dir))
    hub = LiveHub(own_inputs)
    try:
        await hub.handle_bar(bar(T0))
        p = tmp_path / "instruments.yaml"
        p.write_text(p.read_text().replace("last_year: 2030", "last_year: 2025"))
        # armed after the edit: pins the new file, whose NQ calendar ends 2026-03-12
        arming.insert_armed(conn, pinned(replace(armed, account="Sim102"), own_inputs.rules_dir))
        await hub.handle_bar(bar(T0 + timedelta(minutes=1)))
        await hub.handle_bar(bar(T0 + timedelta(minutes=2)))
        first, second = hub.states["Sim101"], hub.states["Sim102"]
        # Sim101 still reads its own calendar: its bars are journalled and its pin halts it
        assert kinds(conn) == ["bar", "action", "halt", "bar", "rejection", "bar", "rejection"]
        assert first.halted == "instruments.yaml changed since arming; re-arm"
        assert second.halted.startswith("live runner error: ValueError: no NQ contract")
        assert kinds(conn, "Sim102") == ["halt"]
    finally:
        await hub.stop()


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

    async def fake_stream(settings, contracts, on_auth=None):
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


async def test_run_bars_closes_the_old_stream_before_the_next_day_subscribes(
    hub, clock, monkeypatch
):
    # LSE allows one socket per key: the new day's subscription must find the old one closed
    monkeypatch.setattr(service, "TICK_EVERY", 0)
    closed: list[bool] = []
    seen: list[list[bool]] = []
    second = asyncio.Event()

    async def fake_stream(settings, contracts, on_auth=None):
        seen.append(list(closed))
        if len(seen) > 1:
            second.set()
            await asyncio.sleep(3600)
        try:
            clock["t"] = T0 + timedelta(days=1)  # the trading day rolls with this bar
            yield bar(T0)
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.05)  # closing a socket takes a moment
            closed.append(True)

    task = asyncio.create_task(run_bars(hub, fake_stream))
    try:
        await asyncio.wait_for(second.wait(), 120)
        assert seen == [[], [True]]
    finally:
        task.cancel()


def lse_alerts(conn, account="Sim101"):
    rows = journal.rows(conn, account=account)
    return [r.payload["message"] for r in rows if r.kind == "alert" and "LSE" in str(r.payload)]


async def test_run_bars_backs_off_after_feed_stopped_and_resets_on_a_bar(hub, monkeypatch):
    monkeypatch.setattr(service, "TICK_EVERY", 0)
    monkeypatch.setattr(service, "FEED_RETRY", 0.01)
    monkeypatch.setattr(service, "FEED_RETRY_MAX", 0.04)
    downs: list[float] = []
    feed_down = hub.feed_down
    monkeypatch.setattr(
        hub, "feed_down", lambda r, at: (downs.append((at - T0).total_seconds()), feed_down(r, at))
    )
    script = ["stop"] * 4 + ["bar"] + ["stop"] * 2 + ["hang"]
    done = asyncio.Event()

    async def fake_stream(settings, contracts, on_auth=None):
        step = script.pop(0)
        if step == "stop":
            raise FeedStopped("LSE stream stopped: Max 1 concurrent websocket connection(s)")
        if step == "bar":
            yield bar(T0)
            return
        done.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(run_bars(hub, fake_stream))
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        task.cancel()
    assert downs == [0.01, 0.02, 0.04, 0.04, 0.01, 0.02]  # doubling, capped, reset by the bar
    assert hub.lse_error is not None and hub.lse_connected is False


async def test_run_bars_ends_on_a_permanent_feed_stop(hub, monkeypatch, caplog):
    monkeypatch.setattr(service, "TICK_EVERY", 0)
    calls = []

    async def fake_stream(settings, contracts, on_auth=None):
        calls.append(contracts)
        raise FeedStopped("FENRIR_LSE_API_KEY is not set", permanent=True)
        yield  # an async generator, like stream_bars

    await asyncio.wait_for(run_bars(hub, fake_stream), 5)
    assert len(calls) == 1
    assert hub.status()["lse"] == {
        "connected": False,
        "last_bar_ts": None,
        "error": "FENRIR_LSE_API_KEY is not set",
        "retry_at": None,
    }
    errors = [r.getMessage() for r in caplog.records if r.name == "fenrir.live.service"]
    assert len(errors) == 1 and "restarts" in errors[0]


async def test_an_empty_key_at_startup_alerts_each_armed_account(hub, conn, armed):
    arming.insert_armed(conn, armed)
    hub.feed_down("FENRIR_LSE_API_KEY is not set", None)  # before the first tick starts accounts
    await hub.tick(T0)
    await hub.tick(T0 + timedelta(seconds=5))
    assert lse_alerts(conn) == [
        "LSE feed down: FENRIR_LSE_API_KEY is not set; no live bars until the server restarts"
    ]


async def test_a_feed_outage_alerts_each_account_once_and_shows_in_status(hub, conn, armed):
    arming.insert_armed(conn, armed)
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    await hub.tick(T0)
    reason = "LSE stream stopped: Max 1 concurrent websocket connection(s)"
    for n in range(3):  # three refused retries: one outage
        hub.feed_down(reason, T0 + timedelta(minutes=2**n))
        await hub.tick(T0 + timedelta(seconds=5 * (n + 1)))
    assert hub.status()["lse"] == {
        "connected": False,
        "last_bar_ts": None,
        "error": reason,
        "retry_at": (T0 + timedelta(minutes=4)).isoformat(),
    }
    expected = (
        f"LSE feed down: {reason}; retrying with a backoff (1 to 15 min); see the live status"
    )
    assert lse_alerts(conn) == lse_alerts(conn, "Sim102") == [expected]
    hub.feed_up()  # a bar arrived: the outage is over
    assert hub.status()["lse"]["error"] is None and hub.status()["lse"]["retry_at"] is None
    hub.feed_down(reason, T0 + timedelta(minutes=16))  # a new outage alerts again
    await hub.tick(T0 + timedelta(minutes=15, seconds=5))
    assert lse_alerts(conn) == [expected, expected]


async def test_a_flapping_feed_alerts_at_most_once_per_quiet_window(hub, conn, armed):
    # #212: a feed that streams a bar and is refused again each minute is one long outage
    arming.insert_armed(conn, armed)
    reason = "LSE stream stopped: Max 1 concurrent websocket connection(s)"
    for minute in range(16):  # up, then refused again, every minute
        now = T0 + timedelta(minutes=minute)
        hub.feed_down(reason, now + timedelta(minutes=1))
        await hub.tick(now)
        await hub.tick(now + timedelta(seconds=5))
        hub.feed_up()
    assert len(lse_alerts(conn)) == 2  # at 0 and at 15 min (FEED_ALERT_EVERY)
    hub.feed_down(reason, None)
    await hub.tick(T0 + timedelta(minutes=16))  # within the window of the last alert
    assert len(lse_alerts(conn)) == 2


async def test_an_outage_still_on_after_the_quiet_window_is_alerted_then(hub, conn, armed):
    arming.insert_armed(conn, armed)
    reason = "LSE stream stopped: Max 1 concurrent websocket connection(s)"
    hub.feed_down(reason, T0 + timedelta(minutes=1))
    await hub.tick(T0)
    hub.feed_up()
    hub.feed_down(reason, T0 + timedelta(minutes=2))  # a new outage a minute later
    await hub.tick(T0 + timedelta(minutes=1))
    assert len(lse_alerts(conn)) == 1  # coalesced
    await hub.tick(T0 + timedelta(minutes=15))  # still down when the window ends
    assert len(lse_alerts(conn)) == 2
    await hub.tick(T0 + timedelta(minutes=31))  # but once per outage
    assert len(lse_alerts(conn)) == 2


async def test_an_account_re_armed_during_an_outage_is_alerted_again(hub, conn, armed):
    # #212: the status shows the outage, but the new arming's journal must say so too
    arming.insert_armed(conn, armed)
    hub.feed_down("LSE stream stopped: quota", T0 + timedelta(minutes=1))
    await hub.tick(T0)
    assert len(lse_alerts(conn)) == 1
    arming.delete_armed(conn, "Sim101")
    await hub.tick(T0 + timedelta(seconds=5))
    arming.insert_armed(conn, replace(armed, armed_at=T0 + timedelta(seconds=7)))
    await hub.tick(T0 + timedelta(seconds=10))
    assert len(lse_alerts(conn)) == 2


async def test_run_bars_recovers_from_a_refused_reconnect_on_the_real_sdk(hub, monkeypatch):
    """A local fake LSE socket: the first connection streams a tick and drops; the SDK's
    reconnect is refused before auth (the server still holds the half-open socket), which the
    SDK treats as fatal; run_bars backs off and the third connection streams again."""
    import functools
    import json

    import lse
    from websockets.asyncio.server import serve

    from fenrir.data.live import stream_bars

    monkeypatch.setattr(service, "FEED_RETRY", 0.05)
    hub.settings = replace(hub.settings, lse_api_key="k")
    connections: list[int] = []
    bars_seen: list[Bar] = []
    recovered = asyncio.Event()

    async def handler(ws):
        n = len(connections) + 1
        connections.append(n)
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.recv()  # auth
        if n == 2:
            message = "Max 1 concurrent websocket connection(s) for this API key"
            await ws.send(json.dumps({"type": "error", "message": message}))
            await ws.wait_closed()
            return
        await ws.send(json.dumps({"type": "authenticated", "tier": "t", "symbols": []}))
        await ws.recv()  # subscribe
        await ws.recv()
        ts = f"2026-10-12T14:0{n}:01Z"  # long past: each tick's bar closes at once
        tick = {"type": "tick", "symbol": "NQ.F", "price": 20000.0, "volume": 1, "ts": ts}
        await ws.send(json.dumps(tick))
        if n == 1:
            await asyncio.sleep(0.2)
            ws.transport.abort()  # the network drops
            return
        await ws.wait_closed()

    handle_bar = hub.handle_bar

    async def seen(b):
        bars_seen.append(b)
        if len(bars_seen) == 2:
            recovered.set()
        await handle_bar(b)

    monkeypatch.setattr(hub, "handle_bar", seen)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(lse, "LSE", functools.partial(lse.LSE, url=f"ws://127.0.0.1:{port}"))
        task = asyncio.create_task(run_bars(hub, stream_bars))
        try:
            await asyncio.wait_for(recovered.wait(), 30)  # the SDK waits 3 s before reconnecting
        finally:
            task.cancel()
            await asyncio.wait({task})
    assert connections == [1, 2, 3]
    assert [b.ts.minute for b in bars_seen] == [1, 3]
    assert hub.lse_error is None and hub.lse_connected is True


async def test_an_authenticated_resubscribe_clears_the_error_before_any_bar(hub, monkeypatch):
    """#212: a refusal at Friday 16:58 retried fine at 16:59 must not show until Sunday's first
    bar. A local fake LSE socket on the real SDK: the first connection is refused before auth,
    the second authenticates and sends nothing."""
    import functools
    import json

    import lse
    from websockets.asyncio.server import serve

    from fenrir.data.live import stream_bars

    monkeypatch.setattr(service, "FEED_RETRY", 0.05)
    hub.settings = replace(hub.settings, lse_api_key="k")
    connections: list[int] = []
    subscribed = asyncio.Event()

    async def handler(ws):
        connections.append(len(connections) + 1)
        await ws.send(json.dumps({"type": "welcome"}))
        await ws.recv()  # auth
        if len(connections) == 1:
            message = "Max 1 concurrent websocket connection(s) for this API key"
            await ws.send(json.dumps({"type": "error", "message": message}))
            await ws.wait_closed()
            return
        await ws.send(json.dumps({"type": "authenticated", "tier": "t", "symbols": []}))
        await ws.recv()  # subscribe
        subscribed.set()
        await ws.wait_closed()  # the market is closed: no tick

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(lse, "LSE", functools.partial(lse.LSE, url=f"ws://127.0.0.1:{port}"))
        task = asyncio.create_task(run_bars(hub, stream_bars))
        try:
            await asyncio.wait_for(subscribed.wait(), 30)
            assert connections == [1, 2]
            assert hub.status()["lse"] == {
                "connected": True,
                "last_bar_ts": None,
                "error": None,
                "retry_at": None,
            }
        finally:
            task.cancel()
            await asyncio.wait({task})


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
