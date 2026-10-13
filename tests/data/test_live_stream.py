import asyncio
import logging
from datetime import UTC, datetime, timedelta

import lse
import pytest

from fenrir.core.bars import Bar
from fenrir.core.sessions import NY
from fenrir.data.live import (
    LAG_GRACE,
    MAX_AHEAD,
    FeedStopped,
    Tick,
    _bars_from,
    stream_bars,
)
from fenrir.data.lse import stream_ticks
from fenrir.settings import Settings


def utc(h: int, m: int, s: float = 0) -> datetime:
    return datetime(2026, 10, 8, h, m, tzinfo=UTC) + timedelta(seconds=s)


def collect(source, clock, poll: float = 0.01, on_live=None) -> list[Bar]:
    async def go():
        return [b async for b in _bars_from(source, lambda: clock[0], poll=poll, on_live=on_live)]

    return asyncio.run(go())


# --- _bars_from (plan 04's three tests; the clock moves past LAG_GRACE to close a minute) ---


def test_bars_from_emits_bar_once_minute_ends():
    clock = [utc(14, 30, 1)]

    async def source():
        for t in (
            Tick(utc(14, 30, 1), 100.0, 1, "NQZ26"),
            Tick(utc(14, 30, 40), 101.0, 2, "NQZ26"),
        ):
            clock[0] = t.ts
            yield t
        clock[0] = utc(14, 31) + LAG_GRACE

    assert collect(source(), clock) == [Bar(utc(14, 30), 100.0, 101.0, 100.0, 101.0, 3, "NQZ26")]


def test_bars_from_does_not_emit_partial_bar_on_stream_end():
    clock = [utc(14, 30, 5)]

    async def source():
        yield Tick(utc(14, 30, 5), 100.0, 1, "NQZ26")
        clock[0] = utc(14, 30, 30)  # stream drops mid-minute

    assert collect(source(), clock) == []


def test_bars_from_closes_bar_when_ticks_stop():
    clock = [utc(14, 30, 5)]

    async def source():
        yield Tick(utc(14, 30, 5), 100.0, 1, "NQZ26")
        clock[0] = utc(14, 31, 1) + LAG_GRACE
        await asyncio.sleep(3600)  # market goes quiet
        yield Tick(utc(15, 30), 1.0, 1, "NQZ26")

    async def go():
        gen = _bars_from(source(), lambda: clock[0], poll=0.01)
        bar = await asyncio.wait_for(anext(gen), timeout=2)
        await gen.aclose()
        return bar

    assert asyncio.run(go()) == Bar(utc(14, 30), 100.0, 100.0, 100.0, 100.0, 1, "NQZ26")


# --- _bars_from: feed lag and clock (#73 comment) ---


def test_bars_from_keeps_a_lagging_tick_within_the_grace():
    clock = [utc(14, 30, 10)]

    async def source():
        yield Tick(utc(14, 30, 10), 100.0, 1, "NQZ26")
        clock[0] = utc(14, 31) + LAG_GRACE / 2  # the minute is over on the wall clock ...
        await asyncio.sleep(0.05)  # ... for a few polls
        yield Tick(utc(14, 30, 59.5), 104.0, 2, "NQZ26")  # but this tick was in flight
        clock[0] = utc(14, 31) + LAG_GRACE

    assert collect(source(), clock) == [Bar(utc(14, 30), 100.0, 104.0, 100.0, 104.0, 3, "NQZ26")]


def test_bars_from_drops_a_tick_stamped_well_ahead_of_the_clock():
    clock = [utc(14, 30, 10)]

    async def source():
        yield Tick(utc(14, 30, 10), 100.0, 1, "NQZ26")
        yield Tick(utc(14, 30, 10) + MAX_AHEAD + timedelta(seconds=1), 999.0, 9, "NQZ26")
        yield Tick(utc(14, 30, 10) + MAX_AHEAD, 101.0, 1, "NQZ26")  # just inside: kept
        clock[0] = utc(14, 30, 20)
        yield Tick(utc(14, 30, 20), 102.0, 1, "NQZ26")  # the real ticks still build the bar
        clock[0] = utc(14, 31) + LAG_GRACE

    assert collect(source(), clock) == [Bar(utc(14, 30), 100.0, 102.0, 100.0, 102.0, 3, "NQZ26")]


def test_bars_from_counts_dropped_ticks_in_one_warning_a_minute(caplog):
    clock = [utc(14, 30, 10)]

    async def source():
        yield Tick(utc(14, 30, 10), 100.0, 1, "NQZ26")
        for _ in range(3):
            yield Tick(utc(14, 30, 10) + MAX_AHEAD * 2, 999.0, 1, "NQZ26")  # ahead
        clock[0] = utc(14, 31) + LAG_GRACE
        await asyncio.sleep(0.05)  # the 14:30 bar closes
        for _ in range(2):
            yield Tick(utc(14, 30, 30), 1.0, 1, "NQZ26")  # late: its minute is closed
        clock[0] = utc(14, 31, 20)
        yield Tick(utc(14, 31, 20), 101.0, 1, "NQZ26")

    with caplog.at_level(logging.WARNING, logger="fenrir.data.live"):
        collect(source(), clock)
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == ["live ticks dropped in the last minute: 3 ahead of the clock, 2 late"]


def test_bars_from_reports_dropped_ticks_when_the_stream_ends(caplog):
    clock = [utc(14, 30, 10)]

    async def source():
        for _ in range(2):
            yield Tick(utc(14, 30, 10) + MAX_AHEAD * 2, 999.0, 1, "NQZ26")

    with caplog.at_level(logging.WARNING, logger="fenrir.data.live"):
        collect(source(), clock)
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == ["live ticks dropped in the last minute: 2 ahead of the clock"]


# --- _bars_from: replayed history (#79) ---


def replayed(ts: datetime, price: float, contract: str = "NQZ26") -> Tick:
    return Tick(ts, price, 1, contract, replay=True)


def test_replayed_ticks_build_bars_by_tick_time_not_the_wall_clock():
    clock = [utc(15, 0)]  # the replay runs long after its minutes ended

    async def source():
        yield replayed(utc(14, 30, 5), 100.0)
        await asyncio.sleep(0.05)  # a pause mid-replay closes nothing on the wall clock
        yield replayed(utc(14, 30, 50), 102.0)
        yield replayed(utc(14, 31, 10), 101.0)  # a new minute closes the last
        yield replayed(utc(14, 32, 1), 99.0)

    assert collect(source(), clock) == [
        Bar(utc(14, 30), 100.0, 102.0, 100.0, 102.0, 2, "NQZ26"),
        Bar(utc(14, 31), 101.0, 101.0, 101.0, 101.0, 1, "NQZ26"),
    ]  # 14:32 is still open when the stream ends: never a partial bar


def test_the_first_live_tick_takes_over_the_replayed_minute():
    clock = [utc(14, 31, 20)]
    live: list[str] = []

    async def source():
        yield replayed(utc(14, 30, 5), 100.0)
        yield replayed(utc(14, 31, 2), 101.0)  # the minute the replay ends in
        yield Tick(utc(14, 31, 20), 103.0, 2, "NQZ26")  # live: continues the 14:31 bar
        clock[0] = utc(14, 32) + LAG_GRACE

    assert collect(source(), clock, on_live=live.append) == [
        Bar(utc(14, 30), 100.0, 100.0, 100.0, 100.0, 1, "NQZ26"),
        Bar(utc(14, 31), 101.0, 103.0, 101.0, 103.0, 3, "NQZ26"),
    ]
    assert live == ["NQZ26"]  # each live tick, never a replayed one


def test_a_replayed_minute_before_the_first_live_tick_is_closed_by_it():
    clock = [utc(14, 33, 1)]

    async def source():
        yield replayed(utc(14, 30, 5), 100.0)
        yield Tick(utc(14, 33, 1), 103.0, 1, "NQZ26")
        clock[0] = utc(14, 34) + LAG_GRACE

    assert collect(source(), clock) == [
        Bar(utc(14, 30), 100.0, 100.0, 100.0, 100.0, 1, "NQZ26"),
        Bar(utc(14, 33), 103.0, 103.0, 103.0, 103.0, 1, "NQZ26"),
    ]


def test_a_replay_after_a_reconnect_fills_the_gap_and_repeats_no_bar():
    # the SDK resubscribes with the same `start`: the day replays again from the open
    clock = [utc(14, 30, 5)]

    async def source():
        yield Tick(utc(14, 30, 5), 100.0, 1, "NQZ26")
        clock[0] = utc(14, 31) + LAG_GRACE
        await asyncio.sleep(0.05)  # 14:30 closes on the wall clock; then the link drops
        clock[0] = utc(14, 34, 30)
        for ts, price in ((utc(14, 30, 5), 100.0), (utc(14, 32, 9), 102.0), (utc(14, 33, 9), 99.0)):
            yield replayed(ts, price)  # 14:30 again, then the minutes the drop missed
        yield Tick(utc(14, 34, 30), 104.0, 1, "NQZ26")
        clock[0] = utc(14, 35) + LAG_GRACE

    assert [(b.ts, b.close) for b in collect(source(), clock)] == [
        (utc(14, 30), 100.0),
        (utc(14, 32), 102.0),
        (utc(14, 33), 99.0),
        (utc(14, 34), 104.0),
    ]


def test_a_reconnect_mid_replay_counts_the_open_replayed_minute_once():
    # review of #224: the SDK replays the same start again; the minute still open must restart
    clock = [utc(14, 33)]

    async def source():
        for ts in (utc(14, 30, 5), utc(14, 31, 5), utc(14, 31, 30)):  # the link drops in 14:31
            yield replayed(ts, 100.0)
        for ts in (utc(14, 30, 5), utc(14, 31, 5), utc(14, 31, 30), utc(14, 32, 5)):
            yield replayed(ts, 100.0)
        yield Tick(utc(14, 33), 101.0, 1, "NQZ26")

    assert [(b.ts, b.volume) for b in collect(source(), clock)] == [
        (utc(14, 30), 1),
        (utc(14, 31), 2),
        (utc(14, 32), 1),
    ]


def test_a_reconnect_while_one_series_replays_counts_its_open_minute_once():
    # ES's replayed minute is still open while NQ is live; the reconnect replays ES again
    clock = [utc(14, 31, 10)]

    async def source():
        for ts in (utc(14, 30, 50), utc(14, 31, 5)):
            yield replayed(ts, 6000.0, "ESZ26")  # ES: still replaying, 14:31 open
        yield Tick(utc(14, 31, 10), 20000.0, 1, "NQZ26")  # NQ: live
        for ts in (utc(14, 30, 50), utc(14, 31, 5), utc(14, 32, 1)):
            yield replayed(ts, 6000.0, "ESZ26")  # the reconnect: ES again from the start

    bars = collect(source(), clock)
    es = [(b.ts, b.volume) for b in bars if b.contract == "ESZ26"]
    assert es == [(utc(14, 30), 1), (utc(14, 31), 1)]


# --- stream_ticks: the real SDK Tick through a fake client ---


class FakeLSE:
    ticks: list = []
    error: Exception | None = None
    fatal: str | None = None  # the SDK's last error message before it stops for good
    hang = False  # a live stream never ends on its own
    after = None  # called once the ticks are out
    calls: list = []
    closed: list = []
    errors: list = []

    def __init__(self, api_key):
        self.calls.append(("init", api_key))

    def on(self, event, callback):
        if event == "error":
            self.errors.append(callback)
        return self

    async def stream_async(self, symbols, reconnect=True, start=None):
        self.calls.append(("stream", list(symbols), reconnect, start))
        try:
            for t in self.ticks:
                yield t
            if self.after:
                type(self).after()  # a plain function on the class, not a method
            if self.error:
                raise self.error
            if self.hang:
                await asyncio.Event().wait()
            for callback in self.errors:  # a fatal error: the SDK emits it, then returns
                callback(self.fatal or "")
        finally:
            self.closed.append(True)


@pytest.fixture
def fake_lse(monkeypatch):
    class Fake(FakeLSE):
        ticks, calls, closed, errors = [], [], [], []
        error = fatal = after = None
        hang = False

    monkeypatch.setattr(lse, "LSE", Fake)
    return Fake


def sdk_tick(symbol, price, volume, ts="2026-10-13T14:23:20.389780Z", replay=False):
    return lse.Tick(symbol=symbol, price=price, volume=volume, timestamp=ts, replay=replay)


async def take(gen) -> tuple[list, Exception | None]:
    got = []
    try:
        async for t in gen:
            got.append(t)
    except FeedStopped as e:
        return got, e
    return got, None


def test_stream_ticks_keeps_replayed_ticks_drops_quotes_and_bad_ticks_and_snaps_price(fake_lse):
    fake_lse.ticks = [
        sdk_tick("NQ.F", 30770.0, 1.0, replay=True),  # replayed history (#79): kept, flagged
        sdk_tick("NQ.F", 30770.0, None),  # quote: no volume
        sdk_tick("ES.F", 7851.5, 0.0),  # quote: zero volume
        sdk_tick("ES.F", 7851.5, 1.0, ts=None),  # no time
        sdk_tick("ES.F", 7851.5, 1.0, ts="2026-10-13T14:23:20"),  # naive time
        sdk_tick("MES.F", 7851.5, 1.0),  # not subscribed by us
        sdk_tick("ES.F", None, 1.0),  # no price
        sdk_tick("ES.F", 0.0, 1.0),  # a missing price arrives as the SDK's 0.0
        sdk_tick("ES.F", float("nan"), 1.0),
        sdk_tick("ES.F", float("inf"), 1.0),
        sdk_tick("NQ.F", 30770.1, 4.0, ts="2026-10-13T14:23:20.44+00:00"),
        sdk_tick("ES.F", 7851.750000000001, 2.0, ts="2026-10-13T10:23:21-04:00"),
    ]
    got, _ = asyncio.run(take(stream_ticks(Settings(lse_api_key="k"), ["ESZ26", "NQZ26"])))
    assert got == [
        Tick(datetime(2026, 10, 13, 14, 23, 20, 389780, tzinfo=UTC), 30770.0, 1, "NQZ26", True),
        Tick(datetime(2026, 10, 13, 14, 23, 20, 440000, tzinfo=UTC), 30770.0, 4, "NQZ26"),
        Tick(datetime(2026, 10, 13, 14, 23, 21, tzinfo=UTC), 7851.75, 2, "ESZ26"),
    ]
    assert all(t.ts.tzinfo is UTC for t in got)
    assert fake_lse.calls == [("init", "k"), ("stream", ["ES.F", "NQ.F"], True, None)]


def test_stream_ticks_replays_from_start(fake_lse):
    # #79: LSE replays up to 24 h of ticks from `start` (naive UTC, as candles), then goes live
    start = datetime(2026, 10, 12, 18, 0, tzinfo=NY)
    asyncio.run(take(stream_ticks(Settings(lse_api_key="k"), ["NQZ26"], start=start)))
    assert fake_lse.calls[1] == ("stream", ["NQ.F"], True, "2026-10-12T22:00:00")


def test_stream_ticks_refuses_an_empty_key_and_never_uses_the_sdk_env_var(fake_lse, monkeypatch):
    monkeypatch.setenv("LSE_API_KEY", "someone-elses-key")
    got, stopped = asyncio.run(take(stream_ticks(Settings(lse_api_key=""), ["ESZ26"])))
    assert got == [] and "FENRIR_LSE_API_KEY" in str(stopped) and stopped.permanent
    assert fake_lse.calls == []  # no client built, so no socket on another key


def test_a_fatal_sdk_stop_raises_feed_stopped_with_the_server_message(fake_lse, caplog):
    fake_lse.ticks = [sdk_tick("ES.F", 7851.5, 1.0)]
    fake_lse.fatal = "Max 1 concurrent websocket connection(s) for this API key"
    with caplog.at_level(logging.WARNING, logger="fenrir.data.lse"):
        got, stopped = asyncio.run(take(stream_ticks(Settings(lse_api_key="k"), ["ESZ26"])))
    assert [t.contract for t in got] == ["ESZ26"]
    assert "Max 1 concurrent" in str(stopped) and not stopped.permanent
    assert "Max 1 concurrent" in caplog.text  # the SDK's error events are logged as they come


def test_an_lse_error_raises_feed_stopped(fake_lse):
    fake_lse.ticks = [sdk_tick("ES.F", 7851.5, 1.0)]
    fake_lse.error = lse.LSEError(403, "forbidden")
    got, stopped = asyncio.run(take(stream_ticks(Settings(lse_api_key="k"), ["ESZ26"])))
    assert [t.contract for t in got] == ["ESZ26"]
    assert "forbidden" in str(stopped) and not stopped.permanent


def test_closing_stream_bars_closes_the_sdk_stream(fake_lse):
    # LSE allows one socket per key: a stream left open blocks the next subscription
    fake_lse.hang = True
    fake_lse.ticks = [sdk_tick("ES.F", 7851.5, 1.0, ts="2026-10-08T14:30:01Z")]

    async def go():
        gen = stream_bars(Settings(lse_api_key="k"), ["ESZ26"])
        bar = await asyncio.wait_for(anext(gen), timeout=2)  # its minute is long over
        await gen.aclose()
        assert fake_lse.closed == [True]  # by aclose itself, not the loop's shutdown
        return bar

    assert asyncio.run(go()) == Bar(utc(14, 30), 7851.5, 7851.5, 7851.5, 7851.5, 1, "ESZ26")


def test_stream_bars_builds_front_month_bars_from_lse_ticks(fake_lse, monkeypatch):
    clock = [utc(14, 30, 55)]
    monkeypatch.setattr("fenrir.data.live._now", lambda: clock[0])
    fake_lse.ticks = [
        sdk_tick("ES.F", 7851.500000000001, 1.0, ts="2026-10-08T14:30:01Z"),
        sdk_tick("NQ.F", 30770.1, 2.0, ts="2026-10-08T14:30:02Z"),
        sdk_tick("ES.F", 7852.0, 3.0, ts="2026-10-08T14:30:50Z"),
        sdk_tick("ES.F", 7852.25, 1.0, ts="2026-10-08T14:31:00Z", replay=True),  # still open
    ]
    fake_lse.after = lambda: clock.__setitem__(0, utc(14, 31) + LAG_GRACE)
    fake_lse.hang = True

    async def go():
        gen = stream_bars(Settings(lse_api_key="k"), ["ESZ26", "NQZ26"])
        bars = [await asyncio.wait_for(anext(gen), timeout=5) for _ in range(2)]
        await gen.aclose()
        return bars

    assert sorted(asyncio.run(go()), key=lambda b: b.contract) == [
        Bar(utc(14, 30), 7851.5, 7852.0, 7851.5, 7852.0, 4, "ESZ26"),
        Bar(utc(14, 30), 30770.0, 30770.0, 30770.0, 30770.0, 2, "NQZ26"),
    ]
