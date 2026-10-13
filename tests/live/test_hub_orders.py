"""22.4: the hub posts the core's Sends to the add-on (the fake), journals failed sends, replays
journalled fills on a restart. The hub cannot start sim yet (23.3 stores the start figures), so
`figures` supplies them, and reconciles with a flat book as 22.5 will on `connected`."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from live_helpers import ARMED_AT, fill, reconciled

from fenrir.bridge.client import AddonClient
from fenrir.bridge.fake_addon import FakeAddon, running
from fenrir.core.actions import Bracket, Flatten
from fenrir.core.bars import Bar
from fenrir.live import arming, core, journal, service
from fenrir.live.arming import Armed
from fenrir.live.service import LiveHub
from fenrir.server.app import create_app
from fenrir.settings import Settings
from fenrir.strategy import Position, file_hash

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # Monday 10:00 ET; NQ front month NQZ26
MNQ = "MNQ 12-26"  # NinjaTrader's name for it
START = core.Start(50_000.0, 50_000.0, 50_000.0)
OPEN = core.DayStart(50_000.0, 50_000.0)

ALWAYS = """
from fenrir.core.actions import Bracket
MARKET = "NQ"
PARAMS = {}
def init(params):
    return 0
def step(state, bar, ctx):
    return state + 1, [Bracket("long", 1, bar.close - 10, bar.close + 20)]
"""
BRACKET = Bracket("long", 1, 19990.0, 20020.0)  # what ALWAYS returns on bar(T0)
CID = core.client_id("Sim101", T0, "0", BRACKET)
BODY = {
    "client_id": CID,
    "account": "Sim101",
    "instrument": MNQ,
    "side": "long",
    "qty": 1,
    "stop": 19990.0,
    "target": 20020.0,
}


def bar(ts: datetime) -> Bar:
    return Bar(ts, 20000.0, 20005.0, 19995.0, 20000.0, 10, "NQZ26")


def mnq_fill(qty, price, ts, cid="other", **kw):
    return {**fill(qty, price, ts, cid=cid), "instrument": MNQ, **kw}


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
    path = tmp_path / "always.py"
    path.write_text(ALWAYS)
    a = Armed(
        "Sim101", str(path), file_hash(path), "lucid-flex-50k", "MNQ", "sim", "evaluation", ARMED_AT
    )
    arming.insert_armed(conn, a)
    return a


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(service, "_now", lambda: now["t"])
    return now


@pytest.fixture(autouse=True)
def figures(monkeypatch):
    real = core.start

    def sim_start(setup, now, *rest):
        s, out = real(replace(setup, start=START, day_start=OPEN), now, *rest)
        return reconciled(s), out

    monkeypatch.setattr(core, "start", sim_start)


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


def rows(conn, kind=None):
    return journal.rows(conn, account="Sim101", kind=kind)


def kinds(conn):
    return [r.kind for r in rows(conn)]


def locked(monkeypatch, *refused):
    """sqlite refuses writes of these kinds (all when none) until the returned switch is off."""
    write, on = journal.write, {"on": True}

    def flaky(conn, ts, account, kind, payload):
        if on["on"] and (not refused or kind in refused):
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    monkeypatch.setattr(journal, "write", flaky)
    return on


async def test_a_bracket_reaches_the_addon_with_the_contract_body(hub, conn, armed, fake):
    await hub.handle_bar(bar(T0))
    assert fake.requests == [("/bracket", BODY)]
    [action] = rows(conn, "action")
    assert (action.payload["sent"], action.payload["client_id"]) == (True, CID)
    assert hub.states["Sim101"].working.client_id == CID


async def test_the_working_entry_is_kept_before_the_post(hub, armed, client, monkeypatch):
    """A fast entry fill must find its working entry, or it counts as foreign and is flattened."""
    seen, post = [], client.bracket

    async def spy(*args):
        seen.append(hub.states["Sim101"].working)
        return await post(*args)

    monkeypatch.setattr(client, "bracket", spy)
    await hub.handle_bar(bar(T0))
    assert [w.client_id for w in seen] == [CID]


async def test_a_refused_send_is_journalled_as_alert_and_frees_the_entry(hub, conn, armed, fake):
    fake.rejects = ["insufficient margin"]
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action", "alert"]
    [alert] = rows(conn, "alert")
    assert alert.payload == {"message": f"send {CID} failed: insufficient margin"}
    assert hub.states["Sim101"].working is None


async def test_rows_after_a_refused_send_are_still_written(hub, conn, armed, fake, monkeypatch):
    decide, peak = core.decide, {"peak": 50_100.0, "ts": T0.isoformat()}

    def then_peak(*args):
        s, out = decide(*args)
        return s, (*out, core.Log("peak", peak))

    monkeypatch.setattr(core, "decide", then_peak)
    fake.rejects = ["no"]
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == ["bar", "action", "alert", "peak"]


async def test_a_timeout_is_not_a_refusal(hub, conn, armed, fake):
    """The order may have landed: the entry stays working (no second bracket) until a fill, the
    day roll or reconciliation."""
    fake.delays = [0.5, 0.5]  # past the client's 0.2 s, on both attempts
    await hub.handle_bar(bar(T0))
    assert [path for path, _ in fake.requests] == ["/bracket", "/bracket"]
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} outcome unknown: ")
    assert hub.states["Sim101"].working.client_id == CID


async def test_a_duplicate_with_nothing_at_ninjatrader_frees_the_entry(hub, conn, armed, fake):
    """The first attempt was refused but its reply lost: the retry's `duplicate` hides that."""
    fake.rejects, fake.delays = ["rejected"], [0.5]
    await hub.handle_bar(bar(T0))
    assert fake.requests == [("/bracket", BODY), ("/bracket", BODY)]
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} failed: duplicate, but NinjaTrader")
    assert hub.states["Sim101"].working is None


ENTRY = {
    "account": "Sim101",
    "order_id": "1",
    "client_id": CID,
    "instrument": MNQ,
    "role": "entry",
    "action": "buy",
    "qty": 1,
    "price": 20000.0,
    "state": "Working",
}
HELD = {"account": "Sim101", "instrument": MNQ, "qty": 1, "avg_price": 20000.0}


@pytest.mark.parametrize(("orders", "positions"), [([ENTRY], []), ([], [HELD])])
async def test_a_duplicate_that_landed_stays_working(hub, conn, armed, fake, orders, positions):
    fake.seen.add(CID)  # an earlier attempt (or process) already sent it
    fake.orders, fake.positions = orders, positions
    await hub.handle_bar(bar(T0))
    assert "alert" not in kinds(conn)
    assert hub.states["Sim101"].working.client_id == CID


@pytest.mark.parametrize(
    ("orders", "positions"),
    [
        ([{**ENTRY, "role": "stop"}], []),  # not its entry
        ([{**ENTRY, "client_id": "other"}], []),
        ([], [{**HELD, "instrument": "MES 12-26"}]),  # a position on another instrument
        ([], [{**HELD, "qty": 0}]),
    ],
)
async def test_a_duplicate_is_freed_unless_its_entry_or_a_position_is_there(
    hub, conn, armed, fake, orders, positions
):
    fake.seen.add(CID)
    fake.orders, fake.positions = orders, positions
    await hub.handle_bar(bar(T0))
    [alert] = rows(conn, "alert")
    assert alert.payload["message"].startswith(f"send {CID} failed: duplicate, but NinjaTrader")
    assert hub.states["Sim101"].working is None


@pytest.mark.parametrize("failing", ["orders", "positions"])
async def test_a_duplicate_that_cannot_be_checked_stays_working(
    hub, conn, armed, fake, client, monkeypatch, failing
):
    """Unknown counts as landed: freeing it could let the strategy double the entry."""

    async def down(account):
        raise httpx.ConnectError("add-on unreachable")

    monkeypatch.setattr(client, failing, down)
    fake.seen.add(CID)
    await hub.handle_bar(bar(T0))
    assert "alert" not in kinds(conn)
    assert hub.states["Sim101"].working.client_id == CID


async def test_no_addon_configured_is_a_failed_send(settings, conn, armed):
    hub = LiveHub(settings)
    try:
        await hub.handle_bar(bar(T0))
    finally:
        await hub.stop()
    [alert] = rows(conn, "alert")
    assert "not configured" in alert.payload["message"]
    assert hub.states["Sim101"].working is None


FOREIGN_TS = T0 + timedelta(seconds=30)
FOREIGN = mnq_fill(1, 20000.0, FOREIGN_TS)  # no bracket of ours: no known stop, flattened at once
FLATTEN = {"client_id": core.client_id("Sim101", FOREIGN_TS, "f1", Flatten()), "account": "Sim101"}


async def test_a_timed_out_flatten_is_retried_under_the_same_client_id(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill(FOREIGN)
    assert fake.requests == [("/flatten", FLATTEN)] * 2
    assert hub.states["Sim101"].exiting == FLATTEN["client_id"]
    await hub.tick(T0 + timedelta(seconds=5))  # answered `duplicate`: the first one landed
    await hub.tick(T0 + timedelta(seconds=10))
    assert fake.requests == [("/flatten", FLATTEN)] * 3
    alerts = [r.payload["message"] for r in rows(conn, "alert")]
    assert [m.split(":")[0] for m in alerts] == [
        "flattening",
        f"send {FLATTEN['client_id']} outcome unknown",
    ]


async def test_a_flatten_is_resent_until_it_is_answered(hub, conn, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5] * 4  # the first post and the first resend both time out
    await hub.handle_fill(FOREIGN)
    await hub.tick(T0 + timedelta(seconds=5))
    assert hub.retry["Sim101"].client_id == FLATTEN["client_id"]
    await hub.tick(T0 + timedelta(seconds=10))
    assert fake.requests == [("/flatten", FLATTEN)] * 5
    assert len(rows(conn, "alert")) == 2  # "flattening" and one "outcome unknown"
    assert "Sim101" not in hub.retry


async def test_a_flatten_is_not_retried_once_flat(hub, armed, fake):
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill(FOREIGN)
    await hub.handle_fill(mnq_fill(-1, 20001.0, T0 + timedelta(seconds=40)))
    await hub.tick(T0 + timedelta(seconds=5))
    assert len(fake.requests) == 2
    assert hub.states["Sim101"].exiting is None


async def test_only_this_accounts_instrument_reaches_on_fill(hub, conn, armed):
    await hub.tick(T0)
    await hub.handle_fill({**FOREIGN, "instrument": "MES 12-26"})
    await hub.handle_fill({**FOREIGN, "account": "Sim102"})
    assert kinds(conn) == []
    assert hub.states["Sim101"].position.qty == 0


async def test_dry_runs_take_no_fills(hub, conn, armed):
    arming.insert_armed(conn, replace(armed, account="Dry1", mode="dry"))
    await hub.tick(T0)
    await hub.handle_fill({**FOREIGN, "account": "Dry1"})
    assert [r.kind for r in journal.rows(conn, account="Dry1")] == []
    assert hub.states["Dry1"].position.qty == 0


async def test_only_the_entry_fill_sets_drift_ticks(hub, armed):
    await hub.handle_bar(bar(T0))  # a working entry, then a fill that is not its own
    await hub.handle_fill(mnq_fill(1, 20010.0, T0 + timedelta(minutes=1, seconds=1)))
    assert hub.drift_ticks == {}


async def test_disarming_drops_the_flatten_retry_and_drift_ticks(hub, conn, armed):
    await hub.tick(T0)
    hub.retry["Sim101"] = core.Send(Flatten(), FLATTEN["client_id"])
    hub.drift_ticks["Sim101"] = 1.0
    arming.delete_armed(conn, "Sim101")
    await hub.tick(T0 + timedelta(seconds=5))
    assert (hub.retry, hub.drift_ticks) == ({}, {})


async def test_one_accounts_refused_rows_never_hold_back_anothers_flatten(
    hub, conn, armed, fake, monkeypatch
):
    arming.insert_armed(conn, replace(armed, account="Sim102"))
    await hub.tick(T0)
    fake.delays = [0.5, 0.5]
    await hub.handle_fill({**FOREIGN, "account": "Sim102"})
    write = journal.write

    def locked_101(conn, ts, account, kind, payload):
        if account == "Sim101":
            raise sqlite3.OperationalError("database is locked")
        write(conn, ts, account, kind, payload)

    monkeypatch.setattr(journal, "write", locked_101)
    hub.unwritten["Sim101"] = (core.Log("peak", {"peak": 50_100.0, "ts": T0.isoformat()}),)
    await hub.tick(T0 + timedelta(seconds=5))  # Sim101 ticks first and fails
    assert [path for path, _ in fake.requests] == ["/flatten"] * 3
    assert "Sim101" in hub.unwritten


async def test_status_reports_the_entry_fill_drift_in_ticks(hub, armed):
    await hub.handle_bar(bar(T0))
    assert hub.status()["drift"] == {"ok": True, "ticks": None}
    await hub.handle_fill(mnq_fill(1, 20001.25, T0 + timedelta(minutes=1, seconds=1), cid=CID))
    assert hub.status()["drift"] == {"ok": False, "ticks": 5.0}  # 1.25 points of 0.25


async def test_a_restart_replays_journalled_fills(settings, hub, client, armed):
    await hub.handle_bar(bar(T0))
    await hub.handle_fill(mnq_fill(1, 20000.0, T0 + timedelta(minutes=1, seconds=1), cid=CID))
    await hub.stop()
    restarted = LiveHub(settings, addon=client)
    try:
        await restarted.tick(T0 + timedelta(minutes=2))
        s = restarted.states["Sim101"]
    finally:
        await restarted.stop()
    assert s.position == Position(1, 20000.0, 19990.0, 20020.0)
    assert s.working is None and len(s.fills) == 1


async def test_a_restart_replays_only_todays_rows(settings, hub, client, conn, armed):
    """Rows since arming but from an earlier trading day are not today's book."""
    arming.insert_armed(conn, replace(armed, armed_at=T0 - timedelta(days=1, hours=1)))
    yesterday = T0 - timedelta(days=1)
    journal.write(conn, yesterday, "Sim101", "fill", mnq_fill(1, 20000.0, yesterday))
    await hub.tick(T0)
    assert hub.states["Sim101"].position.qty == 0


async def test_a_bracket_whose_row_was_refused_is_not_sent(hub, armed, fake, monkeypatch):
    locked(monkeypatch, "action")
    await hub.handle_bar(bar(T0))  # the hub halts the account on the journal error
    assert fake.requests == []
    assert hub.states["Sim101"].working is None


async def test_a_flatten_goes_out_while_the_journal_refuses(hub, armed, fake, monkeypatch):
    await hub.tick(T0)
    locked(monkeypatch, "fill")
    with pytest.raises(sqlite3.OperationalError):
        await hub.handle_fill(FOREIGN)
    assert fake.requests == [("/flatten", FLATTEN)]


async def test_refused_peak_rows_and_every_restored_row_after_them_are_retried(
    hub, conn, armed, monkeypatch
):
    """Rows after the first refused one are not tried; the restored kinds among them are kept."""
    on_bar = core.on_bar
    peak = {"peak": 50_100.0, "ts": T0.isoformat()}
    breach = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}

    def fake_on_bar(s, b):
        s, out = on_bar(s, b)
        s = replace(s, halted=breach["reason"], halt_kind="breach")
        return s, (*out, core.Log("peak", peak), core.Log("halt", breach))

    monkeypatch.setattr(core, "on_bar", fake_on_bar)
    await hub.tick(T0)
    switch = locked(monkeypatch, "peak")
    await hub.handle_bar(bar(T0))
    assert kinds(conn) == []  # the halt after the refused peak is not tried yet
    switch["on"] = False
    await hub.tick(T0 + timedelta(seconds=5))
    assert [r.payload for r in rows(conn) if r.kind in ("peak", "halt")][:2] == [peak, breach]


async def test_stop_tries_once_to_flush_queued_rows(hub, conn, armed, monkeypatch):
    breach = {"reason": "breach max_drawdown: x", "kind": "breach", "until": None}
    on_bar = core.on_bar

    def breaching(s, b):
        s, out = on_bar(s, b)
        return replace(s, halted=breach["reason"], halt_kind="breach"), (
            *out,
            core.Log("halt", breach),
        )

    monkeypatch.setattr(core, "on_bar", breaching)
    await hub.tick(T0)
    switch = locked(monkeypatch)
    await hub.handle_bar(bar(T0))
    switch["on"] = False
    await hub.stop()
    assert breach in [r.payload for r in rows(conn, "halt")]


def test_create_app_sets_the_addon_client(tmp_path):
    settings = Settings(private_dir=tmp_path / "private", nt_token="t")
    app = create_app(settings, "k" * 43, "c" * 22, None)
    assert isinstance(app.state.addon, AddonClient)
    assert app.state.live.addon is app.state.addon
    with TestClient(app, base_url="http://localhost:8000"):
        pass
    assert app.state.addon._http.is_closed
    unset = create_app(replace(settings, nt_token=""), "k" * 43, "c" * 22, None)
    assert unset.state.addon is None and unset.state.live.addon is None
