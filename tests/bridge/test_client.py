import asyncio
import base64
import hashlib
import json
import logging
import re
import time

import httpx
import pytest

from fenrir.bridge.client import ACK, EVENT, ORDERS, POSITIONS, Ack, AddonClient, parse
from fenrir.bridge.fake_addon import FakeAddon, now_ts, running

pytestmark = pytest.mark.anyio

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")  # the contract's ts shape
BAD_TOKEN = "wrong-token-7f3a"
ACCOUNT = {
    "id": "Sim101",
    "name": "Sim101",
    "is_sim": True,
    "balance": 50000.0,
    "realized_pnl_today": 0.0,
    "unrealized_pnl": 0.0,
}
POSITION = {"account": "Sim101", "instrument": "NQ 12-26", "qty": -1, "avg_price": 20000.0}
ORDER = {
    "account": "Sim101",
    "order_id": "o1",
    "client_id": "c1",
    "instrument": "NQ 12-26",
    "role": "stop",
    "action": "buy",
    "qty": 1,
    "price": 20010.0,
    "state": "Working",
}


@pytest.fixture
async def addon():
    fake = FakeAddon(accounts=[ACCOUNT], positions=[POSITION], orders=[ORDER], heartbeat_every=0.05)
    async with running(fake) as url:
        client = AddonClient(
            url, fake.token, timeout=0.3, heartbeat_timeout=0.3, reconnect_delay=0.05
        )
        yield fake, client, url
        await client.aclose()


async def until(gen, pred, timeout=5.0):
    """Collect stream messages up to and including the first that matches pred."""
    seen = []
    while not seen or not pred(seen[-1]):
        seen.append(await asyncio.wait_for(anext(gen), timeout))
    return seen


def kinds(msgs):
    return [m["type"] for m in msgs if m["type"] != "heartbeat"]


async def test_reads(addon):
    _, client, _ = addon
    assert await client.accounts() == [ACCOUNT]
    assert await client.positions("Sim101") == [POSITION]
    assert await client.orders("Sim101") == [ORDER]
    assert await client.positions("Other") == []


async def test_each_post_endpoint(addon):
    fake, client, _ = addon
    acks = [
        await client.bracket("c2", "Sim101", "NQ 12-26", "long", 2, 19990.0, 20020.0),
        await client.flatten("c3", "Sim101"),
        await client.cancel("c4", "Sim101", "o1"),
        await client.flatten_all("c5"),
    ]
    assert acks == [Ack(accepted=True, duplicate=False, error=None)] * 4
    bracket = {"client_id": "c2", "account": "Sim101", "instrument": "NQ 12-26", "side": "long"}
    assert fake.requests == [
        ("/bracket", bracket | {"qty": 2, "stop": 19990.0, "target": 20020.0}),
        ("/flatten", {"client_id": "c3", "account": "Sim101"}),
        ("/cancel", {"client_id": "c4", "account": "Sim101", "order_id": "o1"}),
        ("/flatten-all", {"client_id": "c5"}),
    ]


async def test_resent_client_id_is_duplicate(addon):
    _, client, _ = addon
    assert (await client.flatten("c6", "Sim101")).duplicate is False
    assert await client.flatten("c6", "Sim101") == Ack(accepted=False, duplicate=True, error=None)


async def test_retry_after_timeout_is_deduped(addon):
    fake, client, _ = addon
    fake.delays = [1.0]  # the first response comes after the client gave up
    ack = await client.flatten("c7", "Sim101")
    assert ack.duplicate is True
    assert fake.requests == [("/flatten", {"client_id": "c7", "account": "Sim101"})] * 2
    assert fake.seen == {"c7"}


async def test_bad_token_is_not_retried(addon):
    fake, _, url = addon
    client = AddonClient(url, BAD_TOKEN, timeout=0.3)
    with pytest.raises(httpx.HTTPStatusError) as post_err:
        await client.cancel("c8", "Sim101", "o1")
    with pytest.raises(httpx.HTTPStatusError) as get_err:
        await client.accounts()
    await client.aclose()
    assert post_err.value.response.status_code == get_err.value.response.status_code == 401
    assert fake.requests == []
    assert BAD_TOKEN not in str(post_err.value) + str(get_err.value)


async def test_unreachable_raises_http_error():
    async with running(FakeAddon()) as url:
        pass  # the port is closed again
    client = AddonClient(url, "secret", timeout=0.3, retries=2)
    with pytest.raises(httpx.HTTPError):
        await client.accounts()
    with pytest.raises(httpx.HTTPError):
        await client.flatten_all("c9")
    await client.aclose()


async def test_malformed_read_is_a_clear_error(addon):
    fake, client, _ = addon
    fake.positions = [POSITION | {"qty": "1"}]
    with pytest.raises(ValueError, match=r"malformed /positions response: .* at 0\.qty"):
        await client.positions("Sim101")


@pytest.mark.parametrize(
    ("adapter", "raw"),
    [
        (ACK, b"<html>busy</html>"),
        (ACK, b'{"accepted": 1, "duplicate": false, "error": null}'),
        (ACK, b'{"accepted": true, "error": null}'),
        (POSITIONS, b'[{"account": "a", "instrument": "i", "qty": 1, "avg_price": NaN}]'),
        (POSITIONS, b'{"account": "a"}'),
        (EVENT, b'{"type": "heartbeat", "ts": "2026-10-12T13:31:00+00:00"}'),
        (EVENT, b'{"type": "mystery"}'),
        (EVENT, b"[]"),
    ],
)
def test_malformed_payloads_raise(adapter, raw):
    with pytest.raises(ValueError, match="^malformed x: "):
        parse(adapter, raw, "x")


async def test_stream_yields_connected_then_events(addon):
    fake, client, _ = addon
    fill = {
        "type": "fill",
        "account": "Sim101",
        "client_id": "c1",
        "order_id": "o1",
        "instrument": "NQ 12-26",
        "qty": -1,
        "price": 20000.25,
        "commission": 0.5,
        "ts": now_ts(),
    }
    gen = client.stream()
    assert await anext(gen) == {"type": "connected"}
    beat = (await until(gen, lambda m: m["type"] == "heartbeat"))[-1]
    assert TS.match(beat["ts"]) and TS.match(fill["ts"])
    fake.push(fill)
    fake.push({"type": "position"} | POSITION)
    assert (await until(gen, lambda m: m["type"] == "fill"))[-1] == fill
    assert (await until(gen, lambda m: m["type"] == "position"))[-1] == {
        "type": "position"
    } | POSITION
    await gen.aclose()


async def test_drop_then_reconnect(addon):
    fake, client, _ = addon
    gen = client.stream()
    await until(gen, lambda m: m["type"] == "heartbeat")
    fake.kick()
    seen = await until(gen, lambda m: m["type"] == "connected")
    assert kinds(seen) == ["disconnected", "connected"]
    await gen.aclose()


async def test_missing_heartbeats_mean_disconnected(addon):
    fake, client, _ = addon
    gen = client.stream()
    await until(gen, lambda m: m["type"] == "heartbeat")
    fake.heartbeats = False
    seen = await until(gen, lambda m: m["type"] == "disconnected")
    assert "heartbeat" in seen[-1]["error"]
    fake.heartbeats = True
    assert (await until(gen, lambda m: m["type"] == "connected"))[-1] == {"type": "connected"}
    await gen.aclose()


async def test_malformed_message_drops_the_link(addon):
    fake, client, _ = addon
    gen = client.stream()
    assert await anext(gen) == {"type": "connected"}
    fake.push({"type": "fill", "qty": 1})
    seen = await until(gen, lambda m: m["type"] == "connected")
    assert kinds(seen) == ["disconnected", "connected"]
    assert (
        "malformed stream message" in next(m for m in seen if m["type"] == "disconnected")["error"]
    )
    await gen.aclose()


async def test_stream_with_bad_token_keeps_reporting_disconnected(addon, caplog):
    _, _, url = addon
    caplog.set_level(logging.DEBUG)
    client = AddonClient(url, BAD_TOKEN, reconnect_delay=0.05)
    gen = client.stream()
    first, second = await anext(gen), await anext(gen)
    await gen.aclose()
    await client.aclose()
    assert first["type"] == second["type"] == "disconnected"
    assert BAD_TOKEN not in first["error"] + caplog.text


def test_unknown_type_is_not_echoed():
    marker = "x" * 50
    with pytest.raises(ValueError) as err:
        parse(EVENT, f'{{"type": "{marker}"}}', "stream message")
    assert marker not in str(err.value)
    assert "union_tag_invalid" in str(err.value)


async def test_env_proxies_are_ignored(addon, monkeypatch):
    _, _, url = addon
    async with running(FakeAddon()) as dead_proxy:
        pass  # its port is closed again: anything routed through it fails
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, dead_proxy)
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    # default timeout: a dead proxy refuses at once, and a short one flakes under load
    client = AddonClient(url, "secret", retries=1, reconnect_delay=0.05)
    assert await client.accounts() == [ACCOUNT]
    assert (await client.flatten_all("c12")).accepted is True
    gen = client.stream()
    assert await anext(gen) == {"type": "connected"}
    await gen.aclose()
    await client.aclose()


async def test_silent_peer_is_reported_within_heartbeat_timeout():
    """A half-open link: the peer completes the handshake, then never sends or answers again."""

    async def handshake_then_silence(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = re.search(r"(?im)^sec-websocket-key:\s*(\S+)", request)[1]
        guid = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = base64.b64encode(hashlib.sha1(key.encode() + guid).digest()).decode()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        await writer.drain()
        await asyncio.sleep(30)

    server = await asyncio.start_server(handshake_then_silence, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = AddonClient(f"http://127.0.0.1:{port}", "secret", heartbeat_timeout=0.5)
    gen = client.stream()
    assert await anext(gen) == {"type": "connected"}
    start = time.monotonic()
    assert (await asyncio.wait_for(anext(gen), 15))["type"] == "disconnected"
    assert time.monotonic() - start < 0.5 + 4  # the old close_timeout took ~10.5 s
    await gen.aclose()
    await client.aclose()
    server.close()


def test_month_names_are_read_as_contract_names():
    # an add-on whose NinjaTrader reports FullName as "MNQ DEC26" (#265)
    row = POSITION | {"instrument": "MNQ DEC26"}
    assert parse(POSITIONS, json.dumps([row]), "p")[0]["instrument"] == "MNQ 12-26"
    order = ORDER | {"instrument": "MNQ DEC26"}
    assert parse(ORDERS, json.dumps([order]), "o")[0]["instrument"] == "MNQ 12-26"
    for event in (
        POSITION | {"type": "position", "instrument": "MNQ DEC26"},
        ORDER | {"type": "order", "instrument": "MNQ DEC26"},
        {
            "type": "fill",
            "account": "Sim101",
            "client_id": "c1",
            "order_id": "o1",
            "instrument": "MNQ DEC26",
            "qty": 1,
            "price": 20000.0,
            "commission": 0.5,
            "ts": "2026-10-19T15:20:00.250Z",
        },
    ):
        assert parse(EVENT, json.dumps(event), "e")["instrument"] == "MNQ 12-26"
