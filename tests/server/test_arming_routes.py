import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from fenrir.bridge.client import Ack
from fenrir.live import journal
from fenrir.live.service import LiveHub
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.routes import arming as arming_routes
from fenrir.settings import Settings
from fenrir.strategy import file_hash

KEY = "k" * 43
BASE = "http://localhost:8000"
RULES = Path(__file__).parents[2] / "rules"
STRATEGY = (
    'MARKET = "NQ"\nPARAMS = {"n": 1}\n\n\ndef init(params):\n    return None\n\n\n'
    "def step(state, bar, ctx):\n    return state, []\n"
)
TUNING = {
    "start": "2019-01-02",
    "end": "2025-10-09",
    "attempts": 1700,
    "pass_rate": 0.31,
    "p_pass_and_payout": 0.19,
    "expected_gross_per_attempt": 187.5,
}
NT_ACCOUNTS = [
    {"id": "SIM1", "name": "Sim101", "is_sim": True},
    {"id": "LIVE1", "name": "Lucid 1", "is_sim": False},
]
T1 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


RULES_HASH = file_hash(RULES / "lucid" / "flex-50k.yaml")
INSTRUMENTS_HASH = file_hash(Path("instruments.yaml"))


class FakeAddon:
    def __init__(self, down: bool = False, malformed: bool = False):
        self.down = down
        self.malformed = malformed
        self.refuse: str | None = None  # a /flatten refusal's error, e.g. "unmanaged_account"
        self.flattened: list[str] = []

    async def accounts(self):
        if self.down:
            raise httpx.ConnectError("NinjaTrader is down")
        if self.malformed:  # what AddonClient raises for a reply that fails validation
            raise ValueError("accounts[0].is_sim: wrong type")
        return NT_ACCOUNTS

    async def flatten(self, client_id, account):
        if self.down:
            raise httpx.ConnectError("NinjaTrader is down")
        self.flattened.append(account)
        return Ack(False, False, self.refuse) if self.refuse else Ack(True, False, None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        strategies_dir=tmp_path / "strategies",
        rules_dir=RULES,
        private_dir=tmp_path / "private",
    )
    s.strategies_dir.mkdir()
    (s.data_dir / "results").mkdir(parents=True)
    return s


@pytest.fixture
def path(settings) -> Path:
    p = settings.strategies_dir / "orb.py"
    p.write_text(STRATEGY)
    return p


@pytest.fixture
def client(settings) -> TestClient:
    app = create_app(settings, KEY, "c" * 22, None, port=8000)
    app.state.addon = app.state.live.addon = FakeAddon()
    c = TestClient(app, base_url=BASE, headers={"origin": BASE})
    c.cookies.set(SESSION_COOKIE, KEY)
    return c


def report(settings, path, *, contract="MNQ", dll_on=False, key="k1", rules_hash=RULES_HASH):
    r = {
        "key": key,
        "created_at": "2026-10-10T14:00:00Z",
        "strategy": path.stem,
        "strategy_hash": file_hash(path),
        "params": {"n": 1},
        "account": "lucid-flex-50k",
        "rules_hash": rules_hash,
        "contract": contract,
        "data_version": "d",
        "holdout_months": 12,
        "dll_on": dll_on,
        "periods": {"tuning": TUNING, "holdout": None},
        "holdout_gap": False,
        "note": "",
    }
    (settings.data_dir / "results" / f"{key}.json").write_text(json.dumps(r))


PINS = ("dll_on", "rules_hash", "instruments_hash", "start_balance", "peak_balance")


def body(path, **kw):
    return {
        "account": "SIM1",
        "strategy_path": str(path),
        "rules_account": "lucid-flex-50k",
        "contract": "MNQ",
        "mode": "sim",
        "phase": "evaluation",
        "confirm": False,
        "start_balance": 50000.0,
        "peak_balance": 50500.0,
    } | kw


def armed(client):
    return client.get("/api/arming").json()


def test_needs_a_session_and_our_origin(client, path):
    client.cookies.clear()
    assert client.get("/api/arming").status_code == 401
    assert client.post("/api/arming/arm", json=body(path)).status_code == 401
    client.cookies.set(SESSION_COOKIE, KEY)
    foreign = {"origin": "http://evil.example"}
    assert client.post("/api/arming/arm", json=body(path), headers=foreign).status_code == 403
    assert (
        client.post("/api/arming/disarm", json={"account": "SIM1"}, headers=foreign).status_code
        == 403
    )


def test_nothing_armed_before_the_db_exists(client, settings):
    assert armed(client) == []
    assert not (settings.private_dir / "fenrir.db").exists()


def test_sim_arm_with_a_current_backtest_is_listed(client, settings, path):
    report(settings, path)
    res = client.post("/api/arming/arm", json=body(path))
    assert res.status_code == 200
    assert res.json()["strategy_hash"] == file_hash(path)
    assert [(a["account"], a["mode"]) for a in armed(client)] == [("SIM1", "sim")]


def test_an_arm_pins_dll_on_the_hashes_and_the_balances(client, settings, path):
    """DEC-4d: what the runner checks at every start is stored on the row."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    [row] = armed(client)
    assert {k: row[k] for k in PINS} == {
        "dll_on": False,
        "rules_hash": RULES_HASH,
        "instruments_hash": INSTRUMENTS_HASH,
        "start_balance": 50000.0,
        "peak_balance": 50500.0,
    }


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda s, p: None, "no_backtest"),
        (lambda s, p: report(s, p, contract="NQ"), "no_backtest"),
        (lambda s, p: (report(s, p), p.write_text(STRATEGY + "# tweaked\n")), "hash_changed"),
        (lambda s, p: report(s, p, rules_hash="r0"), "rules_changed"),
        (
            lambda s, p: (
                report(s, p),
                (s.data_dir / "accounts.yaml").write_text(
                    yaml.safe_dump({"lucid-flex-50k": {"dll_on": True}})
                ),
            ),
            "dll_mismatch",
        ),
    ],
)
def test_refusals_are_409_and_arm_nothing(client, settings, path, setup, error):
    setup(settings, path)
    res = client.post("/api/arming/arm", json=body(path))
    assert (res.status_code, res.json()["error"]) == (409, error)
    assert armed(client) == []


def test_a_second_arm_is_already_armed_and_keeps_the_row(client, settings, path):
    report(settings, path)
    assert client.post("/api/arming/arm", json=body(path)).status_code == 200
    before = armed(client)
    res = client.post("/api/arming/arm", json=body(path, mode="dry"))
    assert (res.status_code, res.json()["error"]) == (409, "already_armed")
    assert armed(client) == before


def test_live_needs_confirmation_then_arms(client, settings, path):
    """SC4: a live arm shows the backtest summary first; the confirm echoes what it showed."""
    report(settings, path)
    first = client.post("/api/arming/arm", json=body(path, account="LIVE1", mode="live"))
    assert first.status_code == 409
    assert first.json() == {
        "error": "needs_confirmation",
        "summary": TUNING
        | {
            "expected_net_per_attempt": 187.5,
            "holdout_gap": False,
            "strategy_hash": file_hash(path),
            "rules_hash": RULES_HASH,
            "dll_on": False,
            "liquidation_level": 48500.0,  # 50 000 + max(-2000, 500 - 2000)
        },
    }
    assert armed(client) == []
    second = client.post("/api/arming/arm", json=confirm(path, first.json()["summary"]))
    assert second.status_code == 200 and second.json()["mode"] == "live"


def confirm(path, summary, **kw):
    seen = {k: summary[k] for k in ("strategy_hash", "rules_hash", "dll_on")}
    return body(path, account="LIVE1", mode="live", confirm=True) | seen | kw


def test_a_confirm_after_the_strategy_was_swapped_arms_nothing(client, settings, path):
    """The terminal swaps the file and writes a matching report between summary and confirm:
    the confirm names what the user saw, so it is refused with the current summary."""
    report(settings, path)
    seen = client.post("/api/arming/arm", json=body(path, account="LIVE1", mode="live"))
    path.write_text(STRATEGY + "# swapped\n")
    report(settings, path, key="k2")
    res = client.post("/api/arming/arm", json=confirm(path, seen.json()["summary"]))
    assert (res.status_code, res.json()["error"]) == (409, "needs_confirmation")
    assert res.json()["summary"]["strategy_hash"] == file_hash(path)
    assert armed(client) == []


def test_a_confirm_without_the_hashes_arms_nothing(client, settings, path):
    report(settings, path)
    res = client.post(
        "/api/arming/arm", json=body(path, account="LIVE1", mode="live", confirm=True)
    )
    assert (res.status_code, res.json()["error"]) == (409, "needs_confirmation")
    assert armed(client) == []


@pytest.mark.parametrize("where", ["outside", "dotdot", "not_py", "missing", "symlink"], ids=str)
def test_a_path_that_is_not_a_strategy_file_is_400(client, settings, path, tmp_path, where):
    report(settings, path)
    evil = tmp_path / "evil.py"
    evil.write_text("raise SystemExit('imported!')\n")
    strategies = settings.strategies_dir
    target = {
        "outside": evil,
        "dotdot": strategies / ".." / "evil.py",
        "not_py": strategies / "orb.txt",
        "missing": strategies / "nope.py",
        "symlink": strategies / "link.py",
    }[where]
    if where == "not_py":
        target.write_text(STRATEGY)
    if where == "symlink":
        target.symlink_to(path)
    res = client.post("/api/arming/arm", json=body(target))
    assert res.status_code == 400
    assert armed(client) == []


def test_a_symlinked_strategies_dir_is_400(client, settings, tmp_path):
    """The terminal can swap strategies/ for a link to a dir it should not reach (DEC-1)."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "orb.py").write_text(STRATEGY)
    settings.strategies_dir.rmdir()
    settings.strategies_dir.symlink_to(real)
    path = settings.strategies_dir / "orb.py"
    report(settings, path)
    res = client.post("/api/arming/arm", json=body(path))
    assert res.status_code == 400 and "symlink" in res.json()["detail"]
    assert armed(client) == []


def test_unknown_rules_account_is_400(client, settings, path):
    report(settings, path)
    res = client.post("/api/arming/arm", json=body(path, rules_account="lucid-nope-1k"))
    assert res.status_code == 400
    assert armed(client) == []


@pytest.mark.parametrize(
    "addon",
    [FakeAddon(down=True), FakeAddon(malformed=True), None],
    ids=["down", "malformed", "unconfigured"],
)
def test_ninjatrader_down_is_502_for_sim_and_live_but_not_dry(client, settings, path, addon):
    report(settings, path)
    client.app.state.addon = addon
    for mode, account in (("sim", "SIM1"), ("live", "LIVE1")):
        res = client.post("/api/arming/arm", json=body(path, mode=mode, account=account))
        assert (res.status_code, res.json()["error"]) == (502, "nt_unavailable")
    assert armed(client) == []
    dry = body(path, account="paper", mode="dry")
    del dry["start_balance"], dry["peak_balance"]  # ignored for dry runs
    assert client.post("/api/arming/arm", json=dry).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"start_balance": None},
        {"peak_balance": None},
        {"peak_balance": 49999.0},
        {"mode": "Live"},
        {"phase": "eval"},
        {"start_balance": "nan"},
        {"confirm": "yes"},  # strict booleans: no odd spellings of a live confirmation
        {"dll_on": 0},
    ],
    ids=str,
)
def test_bad_body_is_422(client, settings, path, change):
    report(settings, path)
    b = {k: v for k, v in (body(path) | change).items() if v is not None}
    assert client.post("/api/arming/arm", json=b).status_code == 422
    assert armed(client) == []


def test_a_decide_value_error_is_422(client, settings, path, monkeypatch):
    report(settings, path)

    def bad(*_):
        raise ValueError("bad mode")

    monkeypatch.setattr(arming_routes, "decide", bad)
    assert client.post("/api/arming/arm", json=body(path)).status_code == 422


def test_disarm_flattens_removes_the_row_and_journals_a_halt(client, settings, path):
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    res = client.post("/api/arming/disarm", json={"account": "SIM1"})
    assert res.json() == {"ok": True, "error": None}
    # #223: flattened before the hub lock and again under it
    assert client.app.state.live.addon.flattened == ["SIM1", "SIM1"]
    assert armed(client) == []
    conn = journal.open_ro(settings.private_dir / "fenrir.db")
    [halt, alert] = journal.rows(conn, "SIM1")
    conn.close()
    assert (halt.kind, halt.payload) == (
        "halt",
        {"reason": "disarmed", "kind": None, "until": None},
    )
    assert alert.payload == {"message": "disarmed; flattened, working orders cancelled"}
    # nothing armed: nothing journalled
    res = client.post("/api/arming/disarm", json={"account": "SIM1"})
    assert res.json() == {"ok": False, "error": None}


def test_disarm_before_anything_was_armed_creates_no_database(client, settings):
    assert client.post("/api/arming/disarm", json={"account": "SIM1"}).json() == {
        "ok": False,
        "error": None,
    }
    assert not (settings.private_dir / "fenrir.db").exists()


def test_a_disarm_whose_flatten_fails_keeps_the_row_and_says_the_position_may_be_open(
    client, settings, path
):
    """#223, fail closed: NinjaTrader cannot flatten, so the account stays armed and halted."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    client.app.state.live.addon.down = True
    res = client.post("/api/arming/disarm", json={"account": "SIM1"})
    assert res.status_code == 200 and res.json()["ok"] is False
    assert "the position may still be open" in res.json()["error"]
    assert [a["account"] for a in armed(client)] == ["SIM1"]


def _alerts(settings, account="SIM1"):
    with closing(journal.open_ro(settings.private_dir / "fenrir.db")) as conn:
        return [r.payload["message"] for r in journal.rows(conn, account, kind="alert")]


def test_disarm_without_flatten_disarms_only_an_account_the_addon_does_not_manage(
    client, settings, path
):
    """#230: the add-on refuses the flatten as unmanaged_account; the plain disarm keeps the
    row and says `unmanaged`, and only the confirmed `flatten: false` then disarms it."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    client.app.state.live.addon.refuse = "unmanaged_account"
    res = client.post("/api/arming/disarm", json={"account": "SIM1"}).json()
    assert res["ok"] is False and res["unmanaged"] is True
    assert [a["account"] for a in armed(client)] == ["SIM1"]
    res = client.post("/api/arming/disarm", json={"account": "SIM1", "flatten": False})
    assert res.json() == {"ok": True, "error": None}
    assert armed(client) == []
    assert _alerts(settings)[-1].startswith("disarmed without its flatten: the NinjaTrader add-on")


def test_disarm_without_flatten_still_flattens_a_managed_account(client, settings, path):
    """#230: never a way to skip a managed account's flatten: it lands, so it is flattened."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    res = client.post("/api/arming/disarm", json={"account": "SIM1", "flatten": False})
    assert res.json() == {"ok": True, "error": None}
    assert client.app.state.live.addon.flattened == ["SIM1", "SIM1"]
    assert _alerts(settings) == ["disarmed; flattened, working orders cancelled"]


@pytest.mark.parametrize("refuse", ["unknown account", "refused by the broker"])
def test_disarm_without_flatten_keeps_an_account_refused_for_another_reason(
    client, settings, path, refuse
):
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    client.app.state.live.addon.refuse = refuse
    res = client.post("/api/arming/disarm", json={"account": "SIM1", "flatten": False}).json()
    assert res["ok"] is False and "unmanaged" not in res
    assert res["error"].startswith(f"disarm failed: flatten failed: {refuse};")
    assert [a["account"] for a in armed(client)] == ["SIM1"]


@pytest.mark.parametrize("flatten", ["false", 0, None, "no"])
def test_disarm_takes_flatten_only_as_a_strict_boolean(client, flatten):
    res = client.post("/api/arming/disarm", json={"account": "SIM1", "flatten": flatten})
    assert res.status_code == 422


def test_a_misspelt_flatten_key_is_refused_not_dropped(client, settings, path):
    """#230: an unknown key is a 422 and nothing is sent, never a plain disarm that ignored it."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path))
    res = client.post("/api/arming/disarm", json={"account": "SIM1", "flaten": False})
    assert res.status_code == 422
    assert client.app.state.live.addon.flattened == []
    assert [a["account"] for a in armed(client)] == ["SIM1"]


def test_a_failed_disarm_journals_no_disarmed_row_and_keeps_the_row(client, settings, path):
    """The halt row and the delete are one transaction: both or neither."""
    report(settings, path)
    client.post("/api/arming/arm", json=body(path, account="paper", mode="dry"))
    db = settings.private_dir / "fenrir.db"
    with closing(sqlite3.connect(db)) as c:
        c.execute(
            "CREATE TRIGGER no_delete BEFORE DELETE ON armed BEGIN SELECT RAISE(ABORT, 'x'); END"
        )
    res = client.post("/api/arming/disarm", json={"account": "paper"}).json()
    assert res["ok"] is False and "IntegrityError" in res["error"]
    assert [a["account"] for a in armed(client)] == ["paper"]
    with closing(journal.open_ro(db)) as conn:
        assert {"reason": "disarmed", "kind": None, "until": None} not in [
            r.payload for r in journal.rows(conn)
        ]


def test_rearming_stamps_a_fresh_armed_at(client, settings, path, monkeypatch):
    """Halts and peaks are restored since armed_at: a re-arm must start a new window."""
    report(settings, path)
    monkeypatch.setattr(arming_routes, "_now", lambda: T1)
    client.post("/api/arming/arm", json=body(path))
    client.post("/api/arming/disarm", json={"account": "SIM1"})
    monkeypatch.setattr(arming_routes, "_now", lambda: T2)
    assert client.post("/api/arming/arm", json=body(path)).json()["armed_at"] == T2.isoformat()


def test_two_concurrent_arms_arm_once(client, settings, path, monkeypatch):
    report(settings, path)
    both = threading.Barrier(2, timeout=1)
    decide = arming_routes.decide

    def slow(*args):
        # unlocked, both requests meet here having read an empty armed table
        try:
            both.wait()
        except threading.BrokenBarrierError:
            pass
        return decide(*args)

    monkeypatch.setattr(arming_routes, "decide", slow)
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(
            pool.map(lambda _: client.post("/api/arming/arm", json=body(path)).status_code, [0, 1])
        )
    assert codes == [200, 409]
    assert len(armed(client)) == 1


def test_the_hub_picks_up_arm_and_disarm(client, settings, path):
    report(settings, path)
    dry = body(path, account="paper", mode="dry")
    assert client.post("/api/arming/arm", json=dry).status_code == 200

    async def run():
        hub = LiveHub(settings)
        try:
            await hub.tick(datetime.now(UTC))
            started = [a["account"] for a in hub.status()["accounts"]]
            client.post("/api/arming/disarm", json={"account": "paper"})
            await hub.tick(datetime.now(UTC))
            return started, hub.status()["accounts"]
        finally:
            await hub.stop()

    started, after = asyncio.run(run())
    assert (started, after) == (["paper"], [])


@pytest.mark.parametrize("addon", [FakeAddon(down=True), FakeAddon(malformed=True)], ids=str)
def test_accounts_lists_ninjatrader_accounts_or_502(client, addon):
    assert client.get("/api/arming/accounts").json() == NT_ACCOUNTS
    client.app.state.addon = addon
    res = client.get("/api/arming/accounts")
    assert (res.status_code, res.json()) == (502, {"error": "nt_unavailable", "summary": None})
