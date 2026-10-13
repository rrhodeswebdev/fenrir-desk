import sys
import types
from datetime import UTC, date, datetime

import pytest

from fenrir.data.lse import fetch_contract, lse_symbol
from fenrir.data.pull import Window
from fenrir.settings import Settings


def test_lse_symbol_is_the_continuous_series():
    assert lse_symbol("NQZ26") == "NQ.F"
    assert lse_symbol("ESH30") == "ES.F"


def test_fetch_contract_pages_and_cleans(monkeypatch):
    calls = []
    rows = [
        {
            "timestamp": "2025-12-01T14:30:00Z",
            "open": 7827.250000000001,
            "high": 7829.2,
            "low": 7827.0,
            "close": 7827.5,
            "volume": 3.0,
        },
        {
            "timestamp": "2025-12-01T14:31:00Z",
            "open": 7827.5,
            "high": 7829.0,
            "low": 7827.25,
            "close": 7828.75,
            "volume": 2.0,
        },
        {
            "timestamp": "2025-12-01T14:32:00Z",
            "open": 7828.75,
            "high": 7829.0,
            "low": 7828.0,
            "close": 7828.0,
            "volume": 1.0,
        },
    ]

    class FakeLSE:
        def __init__(self, api_key):
            assert api_key == "k"

        def candles(self, symbol, timeframe, start, end, limit, order, dataset):
            calls.append((symbol, timeframe, start, dataset))
            later = [r for r in rows if r["timestamp"][:19] >= start]
            return later[:limit]

    monkeypatch.setitem(sys.modules, "lse", types.SimpleNamespace(LSE=FakeLSE))
    monkeypatch.setattr("fenrir.data.lse.PAGE", 2)
    w = Window("NQ", "NQZ25", date(2025, 12, 1), date(2025, 12, 1))
    df = fetch_contract(Settings(lse_api_key="k"), w)
    assert df["ts"].to_list() == [datetime(2025, 12, 1, 14, m, tzinfo=UTC) for m in (30, 31, 32)]
    assert df["open"].to_list() == [7827.25, 7827.5, 7828.75]
    assert df["high"].to_list() == [7829.25, 7829.0, 7829.0]  # 7829.2 snaps to 7829.25
    assert df["volume"].to_list() == [3, 2, 1]
    assert [c[0] for c in calls] == ["NQ.F", "NQ.F"]
    assert calls[0][2] == "2025-11-30T23:00:00"  # 18:00 New York (EST) the evening before
    assert calls[1][2] == "2025-12-01T14:32:00"  # resumes one minute after the last row


def test_fetch_contract_refuses_an_empty_key(monkeypatch):
    monkeypatch.setenv("LSE_API_KEY", "someone-elses-key")  # the SDK would fall back to it
    monkeypatch.setitem(sys.modules, "lse", types.SimpleNamespace(LSE=None))
    w = Window("NQ", "NQZ25", date(2025, 12, 1), date(2025, 12, 1))
    with pytest.raises(ValueError, match="FENRIR_LSE_API_KEY"):
        fetch_contract(Settings(), w)
