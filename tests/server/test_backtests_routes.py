import glob
import json
import os
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from backtest_helpers import D1, D2, D3, bar, rules_data
from fastapi.testclient import TestClient
from test_compare import report

from fenrir.backtest.grid import GridItem
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars
from fenrir.server.app import create_app
from fenrir.server.auth import SESSION_COOKIE
from fenrir.server.jobs import Job, to_json
from fenrir.server.jobs_runner import JobRunner
from fenrir.settings import Settings

REPO = Path(__file__).parents[2]
KEY = "k" * 43
BASE = "http://localhost:8000"
COOKIE = {"cookie": f"{SESSION_COOKIE}={KEY}"}
WRITE = COOKIE | {"origin": BASE}
GOOD = {"strategies": ["strategies/*.py"], "accounts": ["lucid-*"], "contracts": None}
SIDE_EFFECT = (  # records the process that executed the file
    "open(__file__ + '.pid', 'w').write(str(__import__('os').getpid()))\n"
    'MARKET = "NQ"\nPARAMS = {}\n\ndef init(p):\n    return None\n\n'
    "def step(s, b, c):\n    return s, []\n"
)


def wait_for(cond, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    """A repo-shaped cwd: strategies/ (relative, as in the CLI), rules, instruments.yaml."""
    monkeypatch.chdir(tmp_path)
    shutil.copy(REPO / "instruments.yaml", tmp_path)
    (tmp_path / "strategies").mkdir()
    shutil.copy(REPO / "strategies" / "orb.py", tmp_path / "strategies")
    (tmp_path / "strategies" / "CLAUDE.md").write_text("not a strategy")
    (tmp_path / "rules" / "lucid").mkdir(parents=True)
    rules = yaml.safe_dump({**rules_data(), "account": "flex-50k"})
    (tmp_path / "rules" / "lucid" / "flex-50k.yaml").write_text(rules)
    return Settings(
        data_dir=tmp_path / "data",
        rules_dir=tmp_path / "rules",
        strategies_dir=Path("strategies"),
        holdout_months=0,
    )


class FakeRunner:
    def __init__(self, grid=(), error: str | None = None) -> None:
        self.calls: list = []
        self.grid, self.error = grid, error

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def expand(self, strategies, accounts, contracts):
        self.calls.append(("expand", strategies, accounts, contracts))
        if self.error:
            raise ValueError(self.error)
        return self.grid

    def submit(self, grid, include_holdout):
        self.calls.append(("submit", grid, include_holdout))
        return Job("j1", "2026-10-11T00:00:00.000000Z", include_holdout, ())


def _app(settings: Settings, runner=None):
    app = create_app(settings, KEY, "c" * 22, None, port=8000)
    if runner is not None:
        app.state.jobs = runner
    return app, TestClient(app, base_url=BASE)


def test_options_lists_files_without_executing_them(settings):
    (settings.strategies_dir / "side.py").write_text(SIDE_EFFECT)
    body = _app(settings)[1].get("/api/backtests/options", headers=COOKIE).json()
    assert body == {
        "strategies": ["strategies/orb.py", "strategies/side.py"],
        "accounts": ["lucid-flex-50k"],
        "contracts": [s.symbol for s in load_instruments().specs],
    }
    assert not (settings.strategies_dir / "side.py.pid").exists()


def test_a_symlinked_strategies_dir_is_500_listing_nothing(settings, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")  # a dir the terminal user cannot read
    (elsewhere / "secret_name.py").write_text("x = 1")
    linked = replace(settings, strategies_dir=Path("linked"))
    linked.strategies_dir.symlink_to(elsewhere, target_is_directory=True)
    r = _app(linked)[1].get("/api/backtests/options", headers=COOKIE)
    assert r.status_code == 500
    assert r.json() == {"detail": "linked is a symlink; not reading it"}


def test_a_planted_instruments_yaml_is_500_naming_it_without_leaking(settings, tmp_path_factory):
    secret = tmp_path_factory.mktemp("root") / "secret.yaml"  # parses as instruments
    secret.write_text((REPO / "instruments.yaml").read_text() + "\n# secret-0001\n")
    Path("instruments.yaml").unlink()
    Path("instruments.yaml").symlink_to(secret)
    r = _app(settings)[1].get("/api/backtests/options", headers=COOKIE)
    assert r.status_code == 500
    assert r.json() == {"detail": "instruments.yaml is a symlink; not reading it"}
    assert "secret-0001" not in r.text


def test_options_paths_pass_the_post_check_with_an_absolute_strategies_dir(settings, tmp_path):
    absolute = replace(settings, strategies_dir=tmp_path / "strategies")
    runner = FakeRunner()
    client = _app(absolute, runner)[1]
    body = client.get("/api/backtests/options", headers=COOKIE).json()
    assert body["strategies"] == ["strategies/orb.py"]
    post = GOOD | {"strategies": body["strategies"]}
    assert client.post("/api/backtests/jobs", headers=WRITE, json=post).status_code == 200


def test_every_listed_strategy_is_accepted_with_strategies_outside_the_cwd(
    settings, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("elsewhere") / "strategies"
    outside.mkdir()
    shutil.copy(REPO / "strategies" / "orb.py", outside)
    shutil.copy(REPO / "strategies" / "orb.py", outside / "orb[1].py")  # a glob char in the name
    (outside / "evil.py").write_text(SIDE_EFFECT)
    away = replace(settings, strategies_dir=outside)
    runner = FakeRunner()
    client = _app(away, runner)[1]
    listed = client.get("/api/backtests/options", headers=COOKIE).json()["strategies"]
    assert listed == [glob.escape(str(outside / n)) for n in ("evil.py", "orb.py", "orb[1].py")]
    for pattern in listed:
        body = GOOD | {"strategies": [pattern]}
        r = client.post("/api/backtests/jobs", headers=WRITE, json=body)
        assert r.status_code == 200, r.text
    (outside / "evil.py").unlink()
    # the real expand child globs the listed names: each matches exactly its own file
    grid = JobRunner(away).expand(listed[1:], ["lucid-*"], ["MNQ"])
    assert [g.strategy_path for g in grid] == [outside / "orb.py", outside / "orb[1].py"]
    # still never a path outside it
    (outside.parent / "evil.py").write_text(SIDE_EFFECT)
    (outside / "link.py").symlink_to(outside.parent / "evil.py")
    for pattern in (
        str(outside.parent / "evil.py"),
        str(outside / ".." / "evil.py"),
        str(outside / "link.py"),
        "strategies/orb.py",  # relative ones resolve against the cwd, not strategies_dir
    ):
        body = GOOD | {"strategies": [pattern]}
        r = client.post("/api/backtests/jobs", headers=WRITE, json=body)
        assert r.status_code == 422, pattern
    assert [c[0] for c in runner.calls] == ["expand", "submit"] * 3


def test_a_wildcard_never_reaches_a_file_outside_strategies_dir(settings, tmp_path_factory):
    # strategy_glob_ok passes each pattern: a wildcard part hides the symlink from resolve()
    outside = tmp_path_factory.mktemp("outside")
    (outside / "evil.py").write_text(SIDE_EFFECT)
    (outside / "x.py").write_text(SIDE_EFFECT)
    strategies = settings.strategies_dir
    (strategies / "link.py").symlink_to(outside / "evil.py")
    (strategies / "sub").symlink_to(outside)
    client = _app(settings)[1]  # the real JobRunner and expand child
    inside = f"is not a file inside {strategies.resolve()}"
    for pattern, match in (
        (str(strategies.resolve() / "l*.py"), str(strategies.resolve() / "link.py")),
        ("strategies/*.py", "strategies/link.py"),
        ("strategies/s*/x.py", "strategies/sub/x.py"),
    ):
        r = client.post("/api/backtests/jobs", headers=WRITE, json=GOOD | {"strategies": [pattern]})
        assert r.status_code == 422, (pattern, r.text)
        assert r.json()["detail"] == f"ValueError: {match!r} {inside}"
    for root in (outside, settings.strategies_dir):
        assert not list(root.rglob("*.pid"))  # nothing was executed, not even orb.py
    assert client.get("/api/backtests/jobs", headers=COOKIE).json() == []


def test_create_app_holds_a_job_runner_with_the_session_key(settings, monkeypatch):
    calls = []
    monkeypatch.setattr("fenrir.server.app.JobRunner", lambda s, **kw: calls.append((s, kw)))
    _app(settings)
    assert calls == [(settings, {"secret": KEY})]


def test_lifespan_starts_and_stops_the_runner(settings):
    runner = FakeRunner()
    app, client = _app(settings, runner)
    with client:
        assert runner.calls == ["start"]
    assert runner.calls == ["start", "stop"]


def test_post_expands_in_the_runner_then_submits(settings):
    grid = (GridItem(Path("strategies/orb.py"), "lucid-flex-50k", "NQ"),)
    runner = FakeRunner(grid)
    r = _app(settings, runner)[1].post("/api/backtests/jobs", headers=WRITE, json=GOOD)
    assert (r.status_code, r.json()) == (200, {"job_id": "j1"})
    assert runner.calls == [
        ("expand", ["strategies/*.py"], ["lucid-*"], None),
        ("submit", grid, True),
    ]


@pytest.mark.parametrize(
    "pattern",
    ["../x.py", "strategies/../x.py", "x.py", "rules/*", "strategies*/x.py", "", "a\x00.py"],
)
def test_a_strategy_outside_strategies_is_422_and_nothing_runs(settings, pattern):
    runner = FakeRunner()
    body = GOOD | {"strategies": ["strategies/orb.py", pattern]}
    r = _app(settings, runner)[1].post("/api/backtests/jobs", headers=WRITE, json=body)
    assert r.status_code == 422
    assert "inside strategies" in r.json()["detail"]
    assert runner.calls == []


def test_an_absolute_or_symlinked_strategy_outside_is_422(settings, tmp_path):
    (tmp_path / "evil.py").write_text(SIDE_EFFECT)
    (settings.strategies_dir / "link.py").symlink_to(tmp_path / "evil.py")
    runner = FakeRunner()
    client = _app(settings, runner)[1]
    outside = (str(tmp_path / "evil.py"), str(tmp_path / "strategies" / ".." / "evil.py"))
    for pattern in (*outside, "strategies/link.py"):
        body = GOOD | {"strategies": [pattern]}
        assert client.post("/api/backtests/jobs", headers=WRITE, json=body).status_code == 422
    assert runner.calls == []


def test_an_expand_error_is_422_with_its_message(settings):
    runner = FakeRunner(error="ValueError: 'strategies/nope.py' matches nothing")
    body = GOOD | {"strategies": ["strategies/nope.py"]}
    r = _app(settings, runner)[1].post("/api/backtests/jobs", headers=WRITE, json=body)
    assert (r.status_code, r.json()["detail"]) == (422, runner.error)
    assert [c[0] for c in runner.calls] == ["expand"]


def test_a_second_expand_while_one_runs_is_409(settings):
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeRunner):
        def expand(self, *args):
            entered.set()
            release.wait(10)
            return super().expand(*args)

    runner = Slow()
    client = _app(settings, runner)[1]
    first: list = []
    post = lambda: client.post("/api/backtests/jobs", headers=WRITE, json=GOOD)  # noqa: E731
    thread = threading.Thread(target=lambda: first.append(post()))
    thread.start()
    try:
        assert entered.wait(10)
        r = post()
        assert (r.status_code, r.json()) == (409, {"detail": "a grid is already being expanded"})
    finally:
        release.set()
        thread.join(10)
    assert first[0].status_code == 200
    assert post().status_code == 200  # the slot is free again


def test_a_malformed_body_is_422(settings):
    runner = FakeRunner()
    client = _app(settings, runner)[1]
    for body in ({"strategies": ["strategies/orb.py"], "accounts": ["x"]}, [], {"x": 1}):
        assert client.post("/api/backtests/jobs", headers=WRITE, json=body).status_code == 422
    assert runner.calls == []


def test_auth(settings):
    runner = FakeRunner()
    client = _app(settings, runner)[1]
    for path in ("options", "jobs", "jobs/x", "results", "compare?view=account"):
        assert client.get(f"/api/backtests/{path}").status_code == 401
    post = "/api/backtests/jobs"
    assert client.post(post, headers={"origin": BASE}, json=GOOD).status_code == 401
    assert client.post(post, headers=COOKIE, json=GOOD).status_code == 403
    evil = COOKIE | {"origin": "http://evil.example"}
    assert client.post(post, headers=evil, json=GOOD).status_code == 403
    assert runner.calls == []


def test_jobs_list_and_get(settings, tmp_path):
    app, client = _app(settings)  # never started: submitted jobs stay pending
    item = (GridItem(tmp_path / "a.py", "lucid-flex-50k", "NQ"),)
    first = app.state.jobs.submit(item, True)
    second = app.state.jobs.submit(item, False)
    as_json = [json.loads(json.dumps(to_json(j))) for j in (second, first)]
    assert client.get("/api/backtests/jobs", headers=COOKIE).json() == as_json
    assert client.get(f"/api/backtests/jobs/{first.id}", headers=COOKIE).json() == as_json[1]
    assert client.get("/api/backtests/jobs/nope", headers=COOKIE).status_code == 404


def _write_reports(settings: Settings, *reports: dict) -> None:
    results = settings.data_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    for r in reports:
        (results / f"{r['key']}.json").write_text(json.dumps(r))


def _choices(settings: Settings, choices: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "accounts.yaml").write_text(yaml.safe_dump(choices))


def test_results_carry_net_and_only_the_full_report_has_sample_equity(settings):
    full = {**report("orb", "a", tuning=(0.2, 50.0), holdout=(0.1, -20.0)), "key": "a" * 64}
    _write_reports(settings, full)
    _choices(settings, {"a": {"eval_price": 30.0}})
    client = _app(settings)[1]
    (row,) = client.get("/api/backtests/results", headers=COOKIE).json()
    for name, net in (("tuning", 20.0), ("holdout", -50.0)):
        assert row["periods"][name]["expected_net_per_attempt"] == net
        assert "sample_equity" not in row["periods"][name]
    one = client.get(f"/api/backtests/results/{'a' * 64}", headers=COOKIE).json()
    assert one["periods"]["tuning"]["sample_equity"] == full["periods"]["tuning"]["sample_equity"]
    assert one["periods"]["holdout"]["expected_net_per_attempt"] == -50.0
    for key in ("b" * 64, "..", "A" * 64):
        assert client.get(f"/api/backtests/results/{key}", headers=COOKIE).status_code == 404


def test_a_corrupt_report_is_500_naming_the_file(settings):
    _write_reports(settings, {**report("orb", "a"), "key": "a" * 64})
    (settings.data_dir / "results" / f"{'b' * 64}.json").write_text("{")
    client = _app(settings)[1]
    for path in ("results", f"results/{'b' * 64}", "compare?view=account"):
        r = client.get(f"/api/backtests/{path}", headers=COOKIE)
        assert r.status_code == 500
        assert "corrupt cached report, delete it" in r.json()["detail"]


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_a_planted_report_is_500_naming_it_without_leaking_or_hanging(settings, kind):
    # data/results is terminal-writable and the root server reads it (DEC-1)
    secret = settings.data_dir / "secret.json"  # a root-only file that parses as a report
    secret.parent.mkdir(parents=True)
    secret.write_text(json.dumps({**report("orb", "a"), "key": "c" * 64, "note": "secret-0001"}))
    planted = settings.data_dir / "results" / f"{'b' * 64}.json"
    planted.parent.mkdir()
    planted.symlink_to(secret) if kind == "symlink" else os.mkfifo(planted)
    client = _app(settings)[1]
    for path in ("results", f"results/{'b' * 64}", "compare?view=account"):
        r = client.get(f"/api/backtests/{path}", headers=COOKIE)
        assert r.status_code == 500 and f"{planted} is " in r.json()["detail"]
        assert "secret-0001" not in r.text
    planted.unlink()
    assert client.get("/api/backtests/results", headers=COOKIE).json() == []


def test_compare_ranks_latest_reports_by_tuning_only(settings):
    _write_reports(
        settings,
        report("low", "a", tuning=(0.1, 10.0), holdout=(0.9, 900.0)),  # hold-out never ranks
        report("high", "a", tuning=(0.3, 10.0)),
        report("tie_rich", "a", tuning=(0.1, 90.0)),
        report("high", "a", tuning=(0.9, 10.0), created="2026-10-01T00:00:00Z"),  # superseded
        report("x", "b"),
    )
    _choices(settings, {"a": {"eval_price": 5.0}})
    client = _app(settings)[1]
    groups = client.get("/api/backtests/compare?view=account", headers=COOKIE).json()
    assert [g["group"] for g in groups] == ["a", "b"]
    rows = groups[0]["rows"]
    assert [r["strategy"] for r in rows] == ["high", "tie_rich", "low"]
    assert rows[0]["periods"]["tuning"]["p_pass_and_payout"] == 0.3
    assert rows[1]["periods"]["tuning"]["expected_net_per_attempt"] == 85.0
    assert rows[2]["periods"]["holdout"]["expected_net_per_attempt"] == 895.0
    assert "sample_equity" not in rows[0]["periods"]["tuning"]
    by_strategy = client.get("/api/backtests/compare?view=strategy", headers=COOKIE).json()
    assert [g["group"] for g in by_strategy] == ["high", "low", "tie_rich", "x"]
    assert client.get("/api/backtests/compare?view=x", headers=COOKIE).status_code == 422


def test_a_real_job_runs_end_to_end(settings):
    code = front_month(load_instruments(), "NQ", D1)
    bars = [
        replace(bar(f"09:{m:02d}", p, p + 2, p - 1, p + 1, day), contract=code)
        for day in (D1, D2, D3)
        for m, p in ((m, 20000.0 + 3 * m) for m in range(30, 60))
    ]
    write_bars(settings.data_dir, "NQ", code, bars)
    app, client = _app(settings)
    assert isinstance(app.state.jobs, JobRunner)
    body = {
        "strategies": ["strategies/orb.py"],
        "accounts": ["lucid-*"],
        "contracts": ["MNQ"],
        "include_holdout": False,
    }
    with client:
        r = client.post("/api/backtests/jobs", headers=WRITE, json=body)
        assert r.status_code == 200, r.text
        url = f"/api/backtests/jobs/{r.json()['job_id']}"

        def done() -> bool:
            return client.get(url, headers=COOKIE).json()["items"][0]["status"] == "done"

        wait_for(done, timeout=120)
        (item,) = client.get(url, headers=COOKIE).json()["items"]
        assert (item["strategy"], item["account"], item["contract"]) == (
            "strategies/orb.py",
            "lucid-flex-50k",
            "MNQ",
        )
        full = client.get(f"/api/backtests/results/{item['key']}", headers=COOKIE).json()
        assert full["periods"]["tuning"]["attempts"] == 3
        assert "sample_equity" in full["periods"]["tuning"]
        assert "expected_net_per_attempt" in full["periods"]["tuning"]
        (row,) = client.get("/api/backtests/results", headers=COOKIE).json()
        assert row["key"] == item["key"] and "sample_equity" not in row["periods"]["tuning"]
