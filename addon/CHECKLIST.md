# Add-on checklist (NinjaTrader Sim101)

The manual check of the add-on (SPEC Success Criterion 6 and the add-on line of SPEC Testing): every line is run by hand on NinjaTrader 8, **Sim101 only**, and recorded with its date, the NinjaTrader version (Control Center → **Help → About**) and the result (`pass`, or `fail` and what happened). A line that cannot be run says why in Result. **Status: not run yet.**

Install and config are in [INSTALL.md](INSTALL.md); this file only checks them.

## Run

- Run by: ______ · Windows version: ______ · PC time zone: ______
- NinjaTrader version: ______ · add-on commit (`git rev-parse --short HEAD` when the zip was built): ______
- Fenrir setup (Docker on the NinjaTrader machine, `http://host.docker.internal:8123`; or a Mac or Linux host with NinjaTrader in a Windows VM, `http://<VM IP>:8123`): ______

## Setup

- **Sim101 only.** `managed_accounts` is `["Sim101"]` for the whole checklist; never list a live account here.
- **Fills.** Connect NinjaTrader to **Connections → Simulated Data Feed** (any time), or a live data connection while the market is open, so Sim101 market orders fill.
- **Instrument.** The examples use `MNQ 12-26`; use the current front month. `P` is its last price rounded to a whole point; write the prices out by hand (`<P-20>` means P minus 20).
- **Logs.** Open **New → NinjaScript Output**. Every add-on line starts with `Fenrir: `. Alerts also go to the Control Center **Log** tab, with a time.
- **Config changes** are read only at start: after each edit of `Documents\NinjaTrader 8\fenrir-addon.json`, restart NinjaTrader.
- **Shell** (bash on the Fenrir machine, from the repo root after `uv sync`; on Windows, Git Bash). `H` is the NinjaTrader address as seen from that machine (`http://localhost:8123` on the NinjaTrader machine itself, `http://<VM IP>:8123` from the VM's host):

```bash
export H=http://<address>:8123 T=<the token>
get()  { curl -s -w "  [%{http_code}]\n" -H "X-Fenrir-Token: $T" "$H/$1"; }
post() { curl -s -w "  [%{http_code}]\n" -H "X-Fenrir-Token: $T" -H "Content-Type: application/json" -d "$2" "$H/$1"; }
```

- **Two helper scripts** (they use Fenrir's own client, which rejects any reply that breaks the contract, so a pass here also checks the JSON shapes). Save them once:

```bash
cat > /tmp/fenrir-watch.py <<'EOF'
# Prints the add-on's /stream events; with ALL=1 also heartbeats and account messages.
# A malformed message shows as {'type': 'disconnected', 'error': 'ValueError: malformed ...'}.
import asyncio
import os

from fenrir.bridge.client import AddonClient


async def main():
    every = os.environ.get("ALL") == "1"
    client = AddonClient(os.environ["H"], os.environ["T"])
    async for msg in client.stream():
        if every or msg["type"] not in ("heartbeat", "account"):
            print(msg, flush=True)


asyncio.run(main())
EOF
cat > /tmp/fenrir-book.py <<'EOF'
# Reconciles NinjaTrader's book for account A against the position Q (signed) you expect on I.
import asyncio
import os

from fenrir.bridge.client import AddonClient
from fenrir.bridge.reconcile import ExpectedBook, NtOrder, NtPosition, reconcile


async def main():
    client = AddonClient(os.environ["H"], os.environ["T"])
    try:
        positions = [NtPosition(**p) for p in await client.positions(os.environ["A"])]
        orders = [NtOrder(**o) for o in await client.orders(os.environ["A"])]
    finally:
        await client.aclose()
    book = ExpectedBook(os.environ["A"], os.environ["I"], int(os.environ["Q"]))
    found = reconcile(book, positions, orders)
    for m in found:
        print(m.kind, "-", m.detail)
    print(f"{len(positions)} positions, {len(orders)} orders:", "mismatch" if found else "matches")


asyncio.run(main())
EOF
```

Watch the stream with `uv run python /tmp/fenrir-watch.py` (Ctrl+C stops it; `ALL=1` in front also prints heartbeats and account messages). It prints `{'type': 'connected'}` on each connect and reconnects by itself after a drop; the printed messages are Fenrir's parsed form, so key order follows the add-on (`type` comes last on `account` and `order` messages); check the book with `A=Sim101 I="MNQ 12-26" Q=0 uv run python /tmp/fenrir-book.py`.

- Use a new `client_id` for every request unless the line says to resend one. The ids below (`ck-c2` …) are examples; after 7 days they may be reused.

## A. Install and import

- [ ] **A1.** On the Fenrir machine: `python3 addon/build.py`, then `unzip -l addon/dist/FenrirAddOn.zip`. **Expect:** `wrote …/addon/dist/FenrirAddOn.zip (3 files)`; the zip holds `AddOns/FenrirAddOn.cs`, `AddOns/FenrirBroker.cs`, `AddOns/FenrirServer.cs` and `Info.xml`. — Date: ______ NT: ______ Result: ______
- [ ] **A2.** On a NinjaTrader with no Fenrir files in `Documents\NinjaTrader 8\bin\Custom\AddOns\`: **Tools → Import → NinjaScript Add-On…** with the zip. **Expect:** it imports and compiles with no error and the three `.cs` files land in `bin\Custom\AddOns\`. If it fails, record the message, install from source (INSTALL.md 1) and continue: the zip layout in `build.py` is then wrong. — Date: ______ NT: ______ Result: ______
- [ ] **A3.** The compile after import (and an **F5** in the NinjaScript Editor). **Expect:** no error about `JavaScriptSerializer` or `System.Web.Extensions` (the zip ships no `AdditionalReferences.txt`, relying on NinjaTrader's default references). — Date: ______ NT: ______ Result: ______
- [ ] **A4.** No config: rename `fenrir-addon.json` away (if present) and restart NinjaTrader. **Expect** in Output: `Fenrir: no …\Documents\NinjaTrader 8\fenrir-addon.json; copy fenrir-addon.example.json there and set the token`, `Fenrir: managed_accounts is empty in fenrir-addon.json: Fenrir will not see or touch any account`, `Fenrir: flattening managed accounts daily at 16:40 New York time`, `Fenrir: set a token of at least 16 characters in fenrir-addon.json; server not started`; the first, second and last also in the Log tab; NinjaTrader keeps running. — Date: ______ NT: ______ Result: ______
- [ ] **A5.** Config as INSTALL.md 2 (token from `openssl rand -hex 32`, `managed_accounts: ["Sim101"]`, `flatten_at: "16:40"`, `flatten_on_disconnect_seconds: null`), the two `netsh` lines of INSTALL.md 3 as Administrator, restart NinjaTrader. **Expect:** `Fenrir: flattening managed accounts daily at 16:40 New York time` and `Fenrir: listening on port 8123`, no alert in the Log tab. — Date: ______ NT: ______ Result: ______
- [ ] **A6.** Without the `urlacl` (skip if NinjaTrader runs as Administrator, which needs none): `netsh http delete urlacl url=http://+:8123/`, restart. **Expect:** `Fenrir: failed to start: System.Net.HttpListenerException … Access is denied` (alert), the 16:40 line still printed. Add the `urlacl` back and restart. — Date: ______ NT: ______ Result: ______
- [ ] **A7.** Short token: `"token": "abcd"`, restart. **Expect:** alert `Fenrir: set a token of at least 16 characters in fenrir-addon.json; server not started`; `get accounts` cannot connect. (I4 checks the flatten still runs.) — Date: ______ NT: ______ Result: ______
- [ ] **A8.** Bad flatten time: `"flatten_at": "4:40pm"`, restart. **Expect:** alert `Fenrir: flatten_at must be "HH:MM" (New York time); using 16:40`, then `… daily at 16:40 New York time`. — Date: ______ NT: ______ Result: ______
- [ ] **A9.** Bad disconnect setting: `"flatten_on_disconnect_seconds": 0` (then `"30"`, a string), restart. **Expect:** alert `Fenrir: flatten_on_disconnect_seconds must be null or a whole number of seconds > 0; it is off` each time. Restore the A5 config. — Date: ______ NT: ______ Result: ______

## B. Connectivity and token

- [ ] **B1.** `curl -s -w "  [%{http_code}]\n" "$H/accounts"` (no token). **Expect:** `{"error":"unauthorized"}  [401]`. — Date: ______ NT: ______ Result: ______
- [ ] **B2.** `curl -s -w "  [%{http_code}]\n" -H "X-Fenrir-Token: wrong" "$H/accounts"`. **Expect:** `{"error":"unauthorized"}  [401]`. — Date: ______ NT: ______ Result: ______
- [ ] **B3.** `get accounts`. **Expect:** `[{"id":"Sim101","name":"Sim101","is_sim":true,"balance":…,"realized_pnl_today":…,"unrealized_pnl":…}]  [200]`, and no other account (Playback101 or any other sim account is left out). — Date: ______ NT: ______ Result: ______
- [ ] **B4.** `get nope` (unknown path, right token). **Expect:** `{"error":"not found"}  [404]`; NinjaTrader keeps running. — Date: ______ NT: ______ Result: ______
- [ ] **B5.** `post flatten 'not json'`. **Expect:** `{"error":"body must be a JSON object"}  [400]`. — Date: ______ NT: ______ Result: ______
- [ ] **B6.** Raw stream: `uv run --with websockets python -m websockets "${H/http/ws}/stream?token=$T"` for 10 s. **Expect:** `{"type":"heartbeat","ts":"YYYY-MM-DDTHH:MM:SS.mmmZ"}` every 2 s (UTC, three decimals, `Z`), each followed by one account message for Sim101, `{"id":"Sim101","name":"Sim101","is_sim":true,…,"type":"account"}` (the add-on writes `type` last there). — Date: ______ NT: ______ Result: ______
- [ ] **B7.** The same with `?token=wrong`. **Expect:** the connection is refused with HTTP 401 before the WebSocket upgrade. — Date: ______ NT: ______ Result: ______
- [ ] **B8.** Fenrir itself: enter the NinjaTrader address and token on the Settings page, `docker compose restart`, open the Live page. **Expect:** the NinjaTrader field shows a heartbeat age of a few seconds (not `no heartbeat yet`). Record the address used. — Date: ______ NT: ______ Result: ______

## C. Orders and brackets

Start flat with no working orders on Sim101.

- [ ] **C1.** `get "positions?account=Sim101"` and `get "orders?account=Sim101"`. **Expect:** `[]  [200]` for both. — Date: ______ NT: ______ Result: ______
- [ ] **C2.** Long bracket: `post bracket '{"client_id":"ck-c2","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":<P-20>,"target":<P+20>}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; in the Orders tab the market entry `F|ck-c2|E` filled, then `F|ck-c2|S` (sell stop market at P-20, 1) and `F|ck-c2|T` (sell limit at P+20, 1) working in one OCO group. — Date: ______ NT: ______ Result: ______
- [ ] **C3.** After C2: `get "positions?account=Sim101"` and `get "orders?account=Sim101"`. **Expect:** one position `{"account":"Sim101","instrument":"MNQ 12-26","qty":1,"avg_price":<fill price>}`; two orders with `"client_id":"ck-c2"`, `"action":"sell"`, `"qty":1`: `"role":"stop","price":<P-20>` and `"role":"target","price":<P+20>`, `state` a working state (e.g. `Accepted`, `Working`). — Date: ______ NT: ______ Result: ______
- [ ] **C4.** Resend the C2 body unchanged. **Expect:** `{"accepted":false,"duplicate":true,"error":null}` and no new order in the Orders tab. — Date: ______ NT: ______ Result: ______
- [ ] **C5.** Flatten: `post flatten '{"client_id":"ck-c5","account":"Sim101"}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; Sim101 flat, stop and target cancelled, Output `Fenrir: MNQ 12-26 flat on Sim101`; C1's reads give `[]` again. — Date: ______ NT: ______ Result: ______
- [ ] **C6.** A second flatten while flat: `post flatten '{"client_id":"ck-c6","account":"Sim101"}'`. **Expect:** `accepted: true` and no order at all (it never over-closes). — Date: ______ NT: ______ Result: ______
- [ ] **C7.** Short bracket: `side "short"`, `stop <P+20>`, `target <P-20>`, id `ck-c7`. **Expect:** accepted; position qty `-1`; stop and target are buy orders (`"action":"buy"` in `/orders`). Then flatten (new id). — Date: ______ NT: ______ Result: ______
- [ ] **C8.** Refusals, each with a new id, nothing placed: long with `stop <P+20>`, `target <P-20>` → `"error":"invalid bracket: stop must be on the losing side of target"`; `"qty":0` → `"invalid bracket: side, qty, stop or target"`; `"instrument":"XYZ 12-26"` → `"unknown instrument"`; a 25-character `client_id` → `"client_id over 24 characters"`; no `client_id` → `"client_id required"`. **Expect:** each `accepted: false, duplicate: false` with that error. — Date: ______ NT: ______ Result: ______
- [ ] **C9.** Unmanaged account: the C2 body with `"account":"Sim102"` and id `ck-c9`. **Expect:** `{"accepted":false,"duplicate":false,"error":"unmanaged_account"}`. Then the same id `ck-c9` on Sim101: **accepted** (not duplicate: refusals are not recorded as seen). Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **C10.** `get "positions?account=Sim102"` and `get "orders?account=Sim102"`. **Expect:** `[]` for both, whatever that account holds (if it exists). — Date: ______ NT: ______ Result: ______
- [ ] **C11.** Cancel a manual order: from Chart Trader or the SuperDOM place a Sim101 buy limit at P-200; `get "orders?account=Sim101"` shows it with `"role":"other"`, `"client_id":""`; `post cancel '{"client_id":"ck-c11","account":"Sim101","order_id":"<its order_id>"}'`. **Expect:** accepted, that order cancelled, nothing else touched. Resend → `duplicate: true`. `"order_id":"nope"` with a new id → `"unknown order"`. — Date: ______ NT: ______ Result: ______
- [ ] **C12.** Rejected stop flattens: take `P` right before sending, then a long bracket with `stop <P+20>`, `target <P+40>` (a sell stop above the market), id `ck-c12`. **Expect:** the entry fills, NinjaTrader rejects the stop, Output alerts `Fenrir: stop rejected for ck-c12: flattening MNQ 12-26 on Sim101` (after `Fenrir: stop ck-c12: <error code> (Rejected)` when NinjaTrader gives one), then `Fenrir: MNQ 12-26 flat on Sim101`; no working orders left. If NinjaTrader accepts the stop instead, record that and flatten. — Date: ______ NT: ______ Result: ______
- [ ] **C13.** Cancelled stop flattens: open a bracket (`ck-c13`), then cancel its stop in the Orders tab. **Expect:** alert `Fenrir: stop cancelled for ck-c13 while the position is held: flattening MNQ 12-26 on Sim101`, then flat with no working orders. — Date: ______ NT: ______ Result: ______
- [ ] **C14.** Cancelled target flattens (fail-safe, #254): open a bracket (`ck-c14`), find its target in `get "orders?account=Sim101"` (the row with `"role":"target"`, `"client_id":"ck-c14"`) and `post cancel` with that `order_id` (new id `ck-c14x`). **Expect:** accepted; NinjaTrader's OCO cancels the stop too, and the add-on treats it as C13: alert `Fenrir: stop cancelled for ck-c14 while the position is held: flattening MNQ 12-26 on Sim101`, then flat with no working orders. If the OCO leaves the stop working instead, that is a fail (the contract says the OCO cancels it): the position stays protected and nothing flattens; flatten by hand and reopen #254 with what NinjaTrader did. — Date: ______ NT: ______ Result: ______
- [ ] **C15.** Stop or target fill: take `P` right before sending, then a bracket with `stop <P-4>`, `target <P+4>` (`ck-c15`), and wait for one to fill (if the stop is rejected the price moved first: that is C12's path, start again). **Expect:** the other is cancelled by the OCO, no `flattening` alert, Sim101 flat. — Date: ______ NT: ______ Result: ______
- [ ] **C16.** Entry still incomplete after 2 s (only when the entry does not fill at once, e.g. a live data connection while the market is closed; otherwise write why not run). **Expect:** `Fenrir: entry ck-c16 not complete after 2 s: cancelling the remainder`; the unfilled rest is cancelled and any part that filled has its stop and target. — Date: ______ NT: ______ Result: ______
- [ ] **C17.** Partial entry fill (only if NinjaTrader's simulator can be made to part-fill a market order; otherwise write why not run). **Expect:** after each part the stop and target quantity equals the total filled so far. — Date: ______ NT: ______ Result: ______
- [ ] **C18.** A submit NinjaTrader refuses (only if one can be forced, e.g. by disconnecting while sending; otherwise write why not run). **Expect:** `"error":"submit failed: …"`, alert `Fenrir: bracket <id> not placed: …`; resending the same id later is **accepted**, not `duplicate`. — Date: ______ NT: ______ Result: ______
- [ ] **C19.** Dedupe survives a restart: flat, restart NinjaTrader. **Expect:** `Fenrir: <n> client ids seen in the last 7 days` with n at least the ids accepted so far; `Documents\NinjaTrader 8\fenrir-addon.seen.json` lists them with UTC times; resending the C2 body → `duplicate: true`, nothing placed. — Date: ______ NT: ______ Result: ______
- [ ] **C20.** Unknown account: add a name NinjaTrader has no account for (e.g. `"SimNone"`) to `managed_accounts`, restart, then the C2 body with `"account":"SimNone"` and id `ck-c20`. **Expect:** `{"accepted":false,"duplicate":false,"error":"unknown account"}`, nothing placed, and `get accounts` still lists Sim101 only. Restore the A5 config. — Date: ______ NT: ______ Result: ______

## D. Fills and the stream

Run `uv run python /tmp/fenrir-watch.py` in a second shell during these lines. No line may print `disconnected` with `malformed`.

- [ ] **D1.** Place a long bracket (`ck-d1`). **Expect:** `{'type': 'fill', 'account': 'Sim101', 'client_id': 'ck-d1', 'order_id': …, 'instrument': 'MNQ 12-26', 'qty': 1, 'price': …, 'commission': …, 'ts': '…Z'}`, `ts` the current UTC time (not NinjaTrader's display time zone) with milliseconds. — Date: ______ NT: ______ Result: ______
- [ ] **D2.** The same bracket: `order` messages for the entry (`'role': 'entry'`, `'state': 'Filled'`, `'qty': 0`), stop and target (remaining `qty` 1), and a `position` message `'qty': 1`. — Date: ______ NT: ______ Result: ______
- [ ] **D3.** Flatten it. **Expect:** a fill with `'qty': -1` and `'client_id': ''` (NinjaTrader's own close), stop and target `order` messages `Cancelled`, and a `position` message `'qty': 0, 'avg_price': 0.0`. — Date: ______ NT: ______ Result: ______
- [ ] **D4.** A short bracket's entry fill. **Expect:** `'qty': -1` with its `client_id`; its stop or target fill carries the same `client_id` with `'qty': 1`. — Date: ______ NT: ______ Result: ______
- [ ] **D5.** A manual Sim101 trade (Chart Trader, no ATM). **Expect:** its fill with `'client_id': ''` and its `order` messages with `'role': 'other'`. Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **D6.** Stalled client (Mac or Linux shell): run two watch scripts, freeze one with `kill -STOP <its pid>`, then place and flatten a bracket (new ids). **Expect:** both acks come back at once (well under a second), and the other watch script still prints the fills, orders and positions. A stalled client is dropped once one send to it takes over 2 s, which may need tens of minutes of messages to fill the socket buffers: record whether and when it was dropped. Check that while the client is still stopped, with `ss -tn | grep 8123` (or `netstat -an | grep 8123`): once dropped, its connection is gone or closing. After `kill -CONT <pid>` the script prints `disconnected` then `connected` either way if it was frozen for longer than the 6 s heartbeat timeout, so that output alone does not show a drop. Ctrl+C both after. — Date: ______ NT: ______ Result: ______

## E. Account messages

- [ ] **E1.** `ALL=1 uv run python /tmp/fenrir-watch.py` for 10 s. **Expect:** after each heartbeat one account message for Sim101, printed as `{'id': 'Sim101', 'name': 'Sim101', 'is_sim': True, …, 'type': 'account'}`, every 2 s, and none for an account outside `managed_accounts`. — Date: ______ NT: ______ Result: ______
- [ ] **E2.** With a position open, compare with the Control Center Accounts tab. **Expect:** `balance` = Cash value, `realized_pnl_today` = Realized PnL, `unrealized_pnl` = Unrealized PnL, the last moving with the price. — Date: ______ NT: ______ Result: ______
- [ ] **E3.** `"managed_accounts": []`, restart. **Expect:** the empty-list alert of A4, `get accounts` → `[]`, the stream sends heartbeats only; `post flatten-all '{"client_id":"ck-e3"}'` → `accepted: true` and touches nothing. Restore `["Sim101"]`. — Date: ______ NT: ______ Result: ______

## F. Reconcile

`/tmp/fenrir-book.py` runs Fenrir's reconcile on what the add-on reports, as the live runner does on start and after every reconnect.

- [ ] **F1.** Flat, no orders, `Q=0`. **Expect:** `0 positions, 0 orders: matches`. — Date: ______ NT: ______ Result: ______
- [ ] **F2.** One long bracket open, `Q=1`. **Expect:** `1 positions, 2 orders: matches`. — Date: ______ NT: ______ Result: ______
- [ ] **F3.** The same bracket, `Q=0`. **Expect:** `position - runner expects 0 MNQ 12-26, NinjaTrader has 1`. Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **F4.** A manual 1-lot buy on MNQ (no stop), `Q=1`. **Expect:** `missing_stop - stop orders cover 0 of 1 contracts` and `missing_target - target orders cover 0 of 1 contracts`. Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **F5.** Flat, a manual buy limit at P-200 working, `Q=0`. **Expect:** `stray_orders - working orders while flat: <its order_id>`. Cancel it after. — Date: ______ NT: ______ Result: ______
- [ ] **F6.** A manual 1-lot on another instrument (e.g. `MES 12-26`), `I="MNQ 12-26" Q=0`. **Expect:** `unexpected_position - MES 12-26 qty 1`. Flatten after. — Date: ______ NT: ______ Result: ______

## G. Kill switch and flatten-all

- [ ] **G1.** Open a long bracket on MNQ and a manual buy limit on `MES 12-26` far from its market (no MES position), then `post flatten-all '{"client_id":"ck-g1"}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; Output `Fenrir: flatten-all ck-g1: flattening Sim101` and `… flat on Sim101` per instrument; Sim101 flat and **every** working order cancelled, the MES limit included. — Date: ______ NT: ______ Result: ______
- [ ] **G2.** Resend `ck-g1`. **Expect:** `duplicate: true`. — Date: ______ NT: ______ Result: ______
- [ ] **G3.** If a sim account outside `managed_accounts` exists (e.g. Sim102), give it a position before G1. **Expect:** G1 leaves it untouched. — Date: ______ NT: ______ Result: ______
- [ ] **G4.** From Fenrir (B8 set up, nothing armed, so the app opens no LSE connection): open a bracket with `post bracket`, then the Live page's **Kill all** → **Flatten and disarm all**. **Expect:** Output `Fenrir: flatten-all kill-<16 hex digits>: flattening Sim101`; Sim101 flat, no working orders. — Date: ______ NT: ______ Result: ______

## H. Lost link

No stream client during these lines unless the line says so: Ctrl+C every watch or `websockets` client, and stop Fenrir (`docker compose stop`).

- [ ] **H1.** Default (`flatten_on_disconnect_seconds: null`): open a bracket, wait 2 minutes with no client. **Expect:** nothing is flattened; stop and target stay working. Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **H2.** `"flatten_on_disconnect_seconds": 30`, restart, start the watch script, open a bracket, Ctrl+C the watch script. **Expect:** about 30 s later the alert `Fenrir: no /stream client for 30 s: flattening Sim101` (or 31, 32), then `… flat on Sim101`, stop and target cancelled. — Date: ______ NT: ______ Result: ______
- [ ] **H3.** Same config, the watch script running for over a minute with a bracket open. **Expect:** no flatten while a client is connected. — Date: ______ NT: ______ Result: ______
- [ ] **H4.** Same config, no client: after H2's flatten open a manual 1-lot. **Expect:** it is flattened within about 2 s (the check repeats every 2 s while no client is connected). — Date: ______ NT: ______ Result: ______
- [ ] **H5.** Same config, counted from start: with a Sim101 position open, restart NinjaTrader and connect no client. **Expect:** the H2 alert about 30 s after `listening on port 8123`. — Date: ______ NT: ______ Result: ______
- [ ] **H6.** Fenrir as the client: Fenrir running (B8), a bracket open, `docker compose stop`. **Expect:** flattened about 30 s later as in H2; after `docker compose start` the Live page's NinjaTrader field shows the heartbeat again. — Date: ______ NT: ______ Result: ______
- [ ] **H7.** Same config with `"token": "abcd"` (server not started), a position open, restart. **Expect:** **not** flattened (the disconnect flatten runs with the server; INSTALL.md 2). Restore the A5 config (`null`) after. — Date: ______ NT: ______ Result: ______

## I. 16:40 flatten (SC6)

For each line Python is disconnected: Fenrir stopped (`docker compose stop`) and no watch or `websockets` client. The test lines set `flatten_at` to New York time now plus 5 minutes, from PowerShell on the NinjaTrader machine:

```powershell
[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time').AddMinutes(5).ToString('HH:mm')
```

Keep a test time before 23:40 New York: the 20-minute window does not run past midnight. Record the Log tab time of each alert and the Executions tab time of each closing fill.

- [ ] **I1.** PC time zone set to anything but Eastern (Windows Settings → Time & language; record it). `flatten_at` = NY now + 5 min, restart (Output: `… daily at HH:MM New York time`). Open a Fenrir bracket on MNQ (`post bracket`), a manual 1-lot on `MES 12-26` with no stop, and a manual buy limit far below the market. **Expect:** within 15 s after HH:MM New York, alert `Fenrir: HH:MM New York flatten: flattening and cancelling on Sim101`, then `… flat on Sim101` per instrument; Sim101 flat with no working orders. — Date: ______ NT: ______ Result: ______
- [ ] **I2.** Inside the 20 minutes after I1's flatten, open a manual 1-lot. **Expect:** flattened again within 15 s, with the I1 alert again. — Date: ______ NT: ______ Result: ______
- [ ] **I3.** More than 20 minutes after HH:MM, open a manual 1-lot. **Expect:** it stays open (the window is over). Flatten it by hand. — Date: ______ NT: ______ Result: ______
- [ ] **I4.** Short token: `"token": "abcd"`, `flatten_at` = NY now + 5 min, restart (A7's alert), open a manual 1-lot. **Expect:** flattened within 15 s after HH:MM, as in I1, though the server never started. — Date: ______ NT: ______ Result: ______
- [ ] **I5.** Failed bind (if A6 could be run): no `urlacl`, `flatten_at` = NY now + 5 min, restart (A6's alert), a manual 1-lot open. **Expect:** flattened as in I1. Restore the `urlacl`. — Date: ______ NT: ______ Result: ______
- [ ] **I6.** If a sim account outside `managed_accounts` exists, give it a position before I1. **Expect:** I1 leaves it untouched. — Date: ______ NT: ______ Result: ______
- [ ] **I7. The real 16:40, default config.** Restore exactly the A5 config (`"flatten_at": "16:40"`, valid token, `flatten_on_disconnect_seconds: null`) and restart. Before 16:40 New York, open a Sim101 position (a Fenrir bracket via `post bracket`, plus a manual 1-lot on another instrument with no stop), then stop Fenrir (`docker compose stop`) and every stream client. Market data running (Simulated Data Feed, or the live feed: CME index futures trade until 17:00 ET). **Expect:** between 16:40:00 and 16:40:15 New York, alert `Fenrir: 16:40 New York flatten: flattening and cancelling on Sim101`, then `… flat on Sim101` per instrument; Sim101 flat, no working orders. Record the PC time zone and the alert and fill times. — Date: ______ NT: ______ Result: ______

## Sign-off

SC6 passes when I1, I2, I4 and I7 pass. The add-on is done (issue #68) when every line above has a date, a NinjaTrader version and a result.

- Signed off by: ______ · Date: ______ · NinjaTrader version: ______
