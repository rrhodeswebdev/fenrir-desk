import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from backtest_helpers import D1, D2, D3, bar, rules_data

from fenrir.backtest import cache
from fenrir.backtest.cache import cache_key, cached_run, list_reports, read_report
from fenrir.backtest.grid import GridItem
from fenrir.backtest.run import run_backtest
from fenrir.core.instruments import front_month, load_instruments
from fenrir.data.store import write_bars
from fenrir.settings import Settings

STRATEGY = (
    'MARKET = "NQ"\nPARAMS = {}\n\ndef init(p):\n    return None\n\n'
    "def step(s, b, c):\n    return s, []\n"
)
ORB = Path(__file__).parents[2] / "strategies" / "orb.py"
SHAPE = {"strategy_hash": "h", "periods": {"tuning": {}, "holdout": None}}  # what readers use


def write_rules(settings, **kw):
    path = settings.rules_dir / "lucid" / "flex-50k.yaml"
    path.write_text(yaml.safe_dump({**rules_data(**kw), "account": "flex-50k"}))


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = replace(
        Settings(),
        data_dir=tmp_path / "data",
        rules_dir=tmp_path / "rules",
        strategies_dir=tmp_path / "strategies",
    )
    s.strategies_dir.mkdir()
    (s.strategies_dir / "s.py").write_text(STRATEGY)
    (s.rules_dir / "lucid").mkdir(parents=True)
    write_rules(s)
    (s.data_dir / "bars").mkdir(parents=True)
    (s.data_dir / "bars" / "manifest.json").write_text("{}")
    return s


@pytest.fixture
def item(settings) -> GridItem:
    return GridItem(settings.strategies_dir / "s.py", "lucid-flex-50k", "MNQ")


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def fake(path, account, contract, s, *, include_holdout, on_progress=None):
        key = cache_key(GridItem(path, account, contract), s, include_holdout)
        calls.append(key)
        created = f"2026-10-10T00:00:{len(calls):02d}Z"
        # a tuple, as a strategy's PARAMS can hold: JSON gives it back as a list
        return SHAPE | {
            "key": key,
            "created_at": created,
            "account": account,
            "params": {"t": (1, 2)},
        }

    monkeypatch.setattr(cache, "run_backtest", fake)
    return calls


def test_second_run_is_cached(settings, item, runs):
    first = cached_run(item, settings, include_holdout=False)
    second = cached_run(item, settings, include_holdout=False)
    assert len(runs) == 1
    assert first == second  # a miss returns the same types as a hit
    assert (settings.data_dir / "results" / f"{first['key']}.json").exists()


def test_any_input_change_misses_the_cache(settings, item, runs):
    keys = {cache_key(item, settings, False)}
    keys.add(cache_key(item, settings, True))
    keys.add(cache_key(replace(item, contract="NQ"), settings, False))
    keys.add(cache_key(item, replace(settings, holdout_months=6), False))
    item.strategy_path.write_text(STRATEGY + "# edited\n")
    keys.add(cache_key(item, settings, False))
    write_rules(settings, target=2500.0)
    keys.add(cache_key(item, settings, False))
    (settings.data_dir / "bars" / "manifest.json").write_text('{"pulled": 2}')
    keys.add(cache_key(item, settings, False))
    accounts = settings.data_dir / "accounts.yaml"
    accounts.write_text(yaml.safe_dump({"lucid-flex-50k": {"dll_on": True}}))
    keys.add(cache_key(item, settings, False))
    assert len(keys) == 8

    cached_run(item, settings, include_holdout=False)
    write_rules(settings, target=2000.0)
    cached_run(item, settings, include_holdout=False)
    assert len(runs) == 2


def test_eval_price_is_not_in_the_key(settings, item):
    before = cache_key(item, settings, False)
    accounts = settings.data_dir / "accounts.yaml"
    accounts.write_text(yaml.safe_dump({"lucid-flex-50k": {"eval_price": 99.0}}))
    assert cache_key(item, settings, False) == before


def test_write_is_atomic_and_temp_files_ignored(settings, item, runs):
    results = settings.data_dir / "results"
    results.mkdir(parents=True)
    (results / "crashed.tmp").write_text('{"half": ')
    cached_run(item, settings, include_holdout=False)
    # only the pre-existing temp file is left beside the report
    assert sorted(p.suffix for p in results.iterdir()) == [".json", ".tmp"]
    assert len(list_reports(settings)) == 1


def test_report_stored_under_its_own_key(settings, item, monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return {"key": "k-of-bytes-that-ran", "created_at": "2026-10-10T00:00:00Z"}

    monkeypatch.setattr(cache, "run_backtest", fake)
    cached_run(item, settings, include_holdout=False)
    assert (settings.data_dir / "results" / "k-of-bytes-that-ran.json").exists()
    cached_run(item, settings, include_holdout=False)
    assert len(calls) == 2  # the item's key never matched what ran


def write_report(settings, key, created_at):
    results = settings.data_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    report = SHAPE | {"key": key, "created_at": created_at, "account": "a"}
    (results / f"{key}.json").write_text(json.dumps(report))
    return report


def test_list_reports_newest_first(settings):
    assert list_reports(settings) == ()
    a = write_report(settings, "a", "2026-10-01T00:00:00Z")
    b = write_report(settings, "b", "2026-10-03T00:00:00Z")
    c = write_report(settings, "c", "2026-10-02T00:00:00Z")
    assert list_reports(settings) == (b, c, a)


def test_list_reports_breaks_created_at_ties_by_key(settings):
    a = write_report(settings, "a", "2026-10-01T00:00:00Z")
    b = write_report(settings, "b", "2026-10-01T00:00:00Z")
    assert list_reports(settings) == (b, a)


BASE = {"key": "k", "created_at": "x", "account": "a"} | SHAPE


@pytest.mark.parametrize(
    "text",
    [
        "",
        '{"half": ',
        '{"key": "k"}',
        '{"created_at": "x"}',
        "[]",
        *(json.dumps({k: v for k, v in BASE.items() if k != gone}) for gone in BASE),
        json.dumps(BASE | {"periods": None}),
        json.dumps(BASE | {"periods": {"tuning": [1]}}),
    ],
)
def test_a_corrupt_report_is_an_error_naming_the_file(settings, item, runs, text):
    path = settings.data_dir / "results" / f"{cache_key(item, settings, False)}.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    for read in (
        lambda: cached_run(item, settings, include_holdout=False),
        lambda: list_reports(settings),
    ):
        with pytest.raises(ValueError, match="corrupt cached report"):
            read()


@pytest.mark.parametrize(
    ("plant", "message"),
    [
        (lambda p, secret: p.symlink_to(secret), "is a symlink"),
        (lambda p, secret: os.mkfifo(p), "is not a regular file"),  # a blocking open would hang
        (lambda p, secret: p.mkdir(), "is not a regular file"),
    ],
    ids=["symlink", "fifo", "directory"],
)
def test_a_planted_report_is_refused_never_followed(settings, plant, message):
    # data/results is terminal-writable and the root server reads it (DEC-1)
    secret = settings.data_dir / "secret.json"  # a root-only file that parses as a report
    secret.write_text(json.dumps(BASE | {"key": "secret-0001"}))
    path = settings.data_dir / "results" / f"{'a' * 64}.json"
    path.parent.mkdir()
    plant(path, secret)
    for read in (lambda: list_reports(settings), lambda: read_report(settings, "a" * 64)):
        with pytest.raises(ValueError, match=f"{path} {message}") as e:
            read()
        assert "secret-0001" not in str(e.value)


def test_an_oversized_report_is_refused(settings, monkeypatch):
    monkeypatch.setattr(cache, "REPORT_MAX", 10)
    path = settings.data_dir / "results" / f"{'a' * 64}.json"
    path.parent.mkdir()
    path.write_text(json.dumps(BASE))
    with pytest.raises(ValueError, match=f"{path} is over 10 bytes"):
        list_reports(settings)


def test_real_run_computes_once(settings, monkeypatch):
    settings = replace(settings, holdout_months=0)
    (settings.data_dir / "bars" / "manifest.json").unlink()
    code = front_month(load_instruments(), "NQ", D1)
    bars = [
        replace(bar(f"09:{m:02d}", p, p + 2, p - 1, p + 1, day), contract=code)
        for day in (D1, D2, D3)
        for m, p in ((m, 20000.0 + 3 * m) for m in range(30, 60))
    ]
    write_bars(settings.data_dir, "NQ", code, bars)
    write_rules(settings)
    calls = []

    def counted(*a, **k):
        calls.append(1)
        return run_backtest(*a, **k)

    monkeypatch.setattr(cache, "run_backtest", counted)
    item = GridItem(ORB, "lucid-flex-50k", "MNQ")
    first = cached_run(item, settings, include_holdout=False)
    stored = settings.data_dir / "results" / f"{first['key']}.json"
    before = stored.read_bytes()
    second = cached_run(item, settings, include_holdout=False)
    assert len(calls) == 1
    assert stored.read_bytes() == before
    assert first == second
    assert first["periods"]["tuning"]["attempts"] == 3
