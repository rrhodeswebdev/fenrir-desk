# Add-on checklist (NinjaTrader Sim101)

The manual check of the add-on (SPEC Success Criterion 6 and the add-on line of SPEC Testing): every line is run by hand on NinjaTrader 8, **Sim101 only**, and recorded with its date, the NinjaTrader version (Control Center → **Help → About**) and the result (`pass`, or `fail` and what happened). A line that cannot be run says why in Result. **Status: not run yet.**

Install and config are in [INSTALL.md](INSTALL.md); this file only checks them.

Trimmed on 2026-10-19 (owner, #68) to the lines that check safety: only managed accounts are touched, the token is required, every position has a stop and losing it flattens, no duplicate orders, flatten never over-closes, the kill switch, the balances risk limits use, reconcile, and the 16:40 flatten. Line ids are kept, so gaps are lines removed: install and config messages, HTTP error replies, stream message shapes, late and partial entries (C16, C17: not checked by hand), and the disconnect flatten. The full list is `addon/CHECKLIST.md` at `b3824c2`; run its H2–H7 there before setting `flatten_on_disconnect_seconds`.

## Run

- Run by: ______ · Windows version: ______ · PC time zone: ______
- NinjaTrader version: ______ · add-on commit (`git rev-parse --short HEAD` when the zip was built): ______
- Fenrir setup (Docker on the NinjaTrader machine, `http://host.docker.internal:8123`; or a Mac or Linux host with NinjaTrader in a Windows VM, `http://<VM IP>:8123`): ______

## Setup

- **Sim101 only.** `managed_accounts` is `["Sim101"]` for the whole checklist; never list a live account here.
- **Fills.** Connect NinjaTrader to **Connections → Simulated Data Feed** (any time), or a live data connection while the market is open, so Sim101 market orders fill.
- **Instrument.** The examples use `MNQ 12-26`; use the current front month. `P` is its last price rounded to a whole point; write the prices out by hand (`<P-20>` means P minus 20). NinjaTrader may name it `MNQ DEC26` (its `FullName`) in Output, the Log tab and the raw `curl` replies below: that is the same contract, and Fenrir's client (so the helper scripts) reads it as `MNQ 12-26` (#265).
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

## A. Config

- [ ] **A5.** Config as INSTALL.md 2 (token from `openssl rand -hex 32`, `managed_accounts: ["Sim101"]`, `flatten_at: "16:40"`, `flatten_on_disconnect_seconds: null`), the two `netsh` lines of INSTALL.md 3 as Administrator, restart NinjaTrader. **Expect:** `Fenrir: flattening managed accounts daily at 16:40 New York time` and `Fenrir: listening on port 8123`, no alert in the Log tab. — Date: ______ NT: ______ Result: ______
- [ ] **A7.** Short token: `"token": "abcd"`, restart. **Expect:** alert `Fenrir: set a token of at least 16 characters in fenrir-addon.json; server not started`; `get accounts` cannot connect. (I4 checks the flatten still runs.) — Date: ______ NT: ______ Result: ______

## B. Connectivity and token

- [ ] **B1.** `curl -s -w "  [%{http_code}]\n" "$H/accounts"` (no token). **Expect:** `{"error":"unauthorized"}  [401]`. — Date: ______ NT: ______ Result: ______
- [ ] **B2.** `curl -s -w "  [%{http_code}]\n" -H "X-Fenrir-Token: wrong" "$H/accounts"`. **Expect:** `{"error":"unauthorized"}  [401]`. — Date: ______ NT: ______ Result: ______
- [ ] **B3.** `get accounts`. **Expect:** `[{"id":"Sim101","name":"Sim101","is_sim":true,"balance":…,"realized_pnl_today":…,"unrealized_pnl":…}]  [200]`, and no other account (Playback101 or any other sim account is left out). — Date: ______ NT: ______ Result: ______
- [ ] **B7.** `uv run --with websockets python -m websockets "${H/http/ws}/stream?token=wrong"`. **Expect:** the connection is refused with HTTP 401 before the WebSocket upgrade. — Date: ______ NT: ______ Result: ______
- [ ] **B8.** Fenrir itself: enter the NinjaTrader address and token on the Settings page, `docker compose restart`, open the Live page. **Expect:** the NinjaTrader field shows a heartbeat age of a few seconds (not `no heartbeat yet`). Record the address used. — Date: ______ NT: ______ Result: ______

## C. Orders and brackets

Start flat with no working orders on Sim101.

- [ ] **C2.** Long bracket: `post bracket '{"client_id":"ck-c2","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":<P-20>,"target":<P+20>}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; in the Orders tab the market entry `F|ck-c2|E` filled, then `F|ck-c2|S` (sell stop market at P-20, 1) and `F|ck-c2|T` (sell limit at P+20, 1) working in one OCO group. — Date: ______ NT: ______ Result: ______
- [ ] **C4.** Resend the C2 body unchanged. **Expect:** `{"accepted":false,"duplicate":true,"error":null}` and no new order in the Orders tab. — Date: ______ NT: ______ Result: ______
- [ ] **C5.** Flatten: `post flatten '{"client_id":"ck-c5","account":"Sim101"}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; Sim101 flat, stop and target cancelled, Output `Fenrir: MNQ 12-26 flat on Sim101`; `get "positions?account=Sim101"` and `get "orders?account=Sim101"` give `[]` again. — Date: ______ NT: ______ Result: ______
- [ ] **C6.** A second flatten while flat: `post flatten '{"client_id":"ck-c6","account":"Sim101"}'`. **Expect:** `accepted: true` and no order at all (it never over-closes). — Date: ______ NT: ______ Result: ______
- [ ] **C7.** Short bracket: `side "short"`, `stop <P+20>`, `target <P-20>`, id `ck-c7`. **Expect:** accepted; position qty `-1`; stop and target are buy orders (`"action":"buy"` in `/orders`). Then flatten (new id). — Date: ______ NT: ______ Result: ______
- [ ] **C8.** Refusals, each with a new id, nothing placed: long with `stop <P+20>`, `target <P-20>` → `"error":"invalid bracket: stop must be on the losing side of target"`; `"qty":0` → `"invalid bracket: side, qty, stop or target"`; `"instrument":"XYZ 12-26"` → `"unknown instrument"`; a 25-character `client_id` → `"client_id over 24 characters"`; no `client_id` → `"client_id required"`. **Expect:** each `accepted: false, duplicate: false` with that error. — Date: ______ NT: ______ Result: ______
- [ ] **C9.** Unmanaged account: the C2 body with `"account":"Sim102"` and id `ck-c9`. **Expect:** `{"accepted":false,"duplicate":false,"error":"unmanaged_account"}`. Then the same id `ck-c9` on Sim101: **accepted** (not duplicate: refusals are not recorded as seen). Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **C12.** Rejected stop flattens: take `P` right before sending, then a long bracket with `stop <P+20>`, `target <P+40>` (a sell stop above the market), id `ck-c12`. **Expect:** the entry fills, NinjaTrader rejects the stop, Output alerts `Fenrir: stop rejected for ck-c12: flattening MNQ 12-26 on Sim101` (after `Fenrir: stop ck-c12: <error code> (Rejected)` when NinjaTrader gives one), then `Fenrir: MNQ 12-26 flat on Sim101`; no working orders left. If NinjaTrader accepts the stop instead, record that and flatten. — Date: ______ NT: ______ Result: ______
- [ ] **C13.** Cancelled stop flattens: open a bracket (`ck-c13`), then cancel its stop in the Orders tab. **Expect:** alert `Fenrir: stop cancelled for ck-c13 while the position is held: flattening MNQ 12-26 on Sim101`, then flat with no working orders. — Date: ______ NT: ______ Result: ______
- [ ] **C14.** Cancelled target flattens (fail-safe, #254): open a bracket (`ck-c14`), find its target in `get "orders?account=Sim101"` (the row with `"role":"target"`, `"client_id":"ck-c14"`) and `post cancel` with that `order_id` (new id `ck-c14x`). **Expect:** accepted; NinjaTrader's OCO cancels the stop too, and the add-on treats it as C13: alert `Fenrir: stop cancelled for ck-c14 while the position is held: flattening MNQ 12-26 on Sim101`, then flat with no working orders. If the OCO leaves the stop working instead, that is a fail (the contract says the OCO cancels it): the position stays protected and nothing flattens; flatten by hand and reopen #254 with what NinjaTrader did. — Date: ______ NT: ______ Result: ______
- [ ] **C15.** Stop or target fill: take `P` right before sending, then a bracket with `stop <P-4>`, `target <P+4>` (`ck-c15`), and wait for one to fill (if the stop is rejected the price moved first: that is C12's path, start again). **Expect:** the other is cancelled by the OCO, no `flattening` alert, Sim101 flat. — Date: ______ NT: ______ Result: ______
- [ ] **C19.** Dedupe survives a restart: flat, restart NinjaTrader. **Expect:** `Fenrir: <n> client ids seen in the last 7 days` with n at least the ids accepted so far; `Documents\NinjaTrader 8\fenrir-addon.seen.json` lists them with UTC times; resending the C2 body → `duplicate: true`, nothing placed. — Date: ______ NT: ______ Result: ______

## E. Account messages

- [ ] **E2.** `ALL=1 uv run python /tmp/fenrir-watch.py` with a position open; compare its account messages with the Control Center Accounts tab. **Expect:** `balance` = Cash value, `realized_pnl_today` = Realized PnL, `unrealized_pnl` = Unrealized PnL, the last moving with the price. — Date: ______ NT: ______ Result: ______

## F. Reconcile

`/tmp/fenrir-book.py` runs Fenrir's reconcile on what the add-on reports, as the live runner does on start and after every reconnect.

- [ ] **F2.** One long bracket open, `Q=1`. **Expect:** `1 positions, 2 orders: matches`. — Date: ______ NT: ______ Result: ______
- [ ] **F3.** The same bracket, `Q=0`. **Expect:** `position - runner expects 0 MNQ 12-26, NinjaTrader has 1`. Flatten after. — Date: ______ NT: ______ Result: ______
- [ ] **F4.** A manual 1-lot buy on MNQ (no stop), `Q=1`. **Expect:** `missing_stop - stop orders cover 0 of 1 contracts` and `missing_target - target orders cover 0 of 1 contracts`. Flatten after. — Date: ______ NT: ______ Result: ______

## G. Kill switch and flatten-all

- [ ] **G1.** Open a long bracket on MNQ and a manual buy limit on `MES 12-26` far from its market (no MES position), then `post flatten-all '{"client_id":"ck-g1"}'`. **Expect:** `{"accepted":true,"duplicate":false,"error":null}`; Output `Fenrir: flatten-all ck-g1: flattening Sim101` and `… flat on Sim101` per instrument; Sim101 flat and **every** working order cancelled, the MES limit included. — Date: ______ NT: ______ Result: ______
- [ ] **G3.** A sim account outside `managed_accounts` (e.g. Sim102; add one in NinjaTrader if there is none): give it a position before G1. **Expect:** G1 leaves it untouched. — Date: ______ NT: ______ Result: ______
- [ ] **G4.** From Fenrir (B8 set up, nothing armed, so the app opens no LSE connection): open a bracket with `post bracket`, then the Live page's **Kill all** → **Flatten and disarm all**. **Expect:** Output `Fenrir: flatten-all kill-<16 hex digits>: flattening Sim101`; Sim101 flat, no working orders. — Date: ______ NT: ______ Result: ______

## H. Lost link

No stream client during these lines unless the line says so: Ctrl+C every watch or `websockets` client, and stop Fenrir (`docker compose stop`).

- [ ] **H1.** Default (`flatten_on_disconnect_seconds: null`): open a bracket, wait 2 minutes with no client. **Expect:** nothing is flattened; stop and target stay working. Flatten after. — Date: ______ NT: ______ Result: ______

## I. 16:40 flatten (SC6)

For each line Python is disconnected: Fenrir stopped (`docker compose stop`) and no watch or `websockets` client. The test lines set `flatten_at` to New York time now plus 5 minutes, from PowerShell on the NinjaTrader machine:

```powershell
[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time').AddMinutes(5).ToString('HH:mm')
```

Keep a test time before 23:40 New York: the 20-minute window does not run past midnight. Record the Log tab time of each alert and the Executions tab time of each closing fill.

- [ ] **I1.** PC time zone set to anything but Eastern (Windows Settings → Time & language; record it). `flatten_at` = NY now + 5 min, restart (Output: `… daily at HH:MM New York time`). Open a Fenrir bracket on MNQ (`post bracket`), a manual 1-lot on `MES 12-26` with no stop, and a manual buy limit far below the market. **Expect:** within 15 s after HH:MM New York, alert `Fenrir: HH:MM New York flatten: flattening and cancelling on Sim101`, then `… flat on Sim101` per instrument; Sim101 flat with no working orders. — Date: ______ NT: ______ Result: ______
- [ ] **I2.** Inside the 20 minutes after I1's flatten, open a manual 1-lot. **Expect:** flattened again within 15 s, with the I1 alert again. — Date: ______ NT: ______ Result: ______
- [ ] **I4.** Short token: `"token": "abcd"`, `flatten_at` = NY now + 5 min, restart (A7's alert), open a manual 1-lot. **Expect:** flattened within 15 s after HH:MM, as in I1, though the server never started. — Date: ______ NT: ______ Result: ______
- [ ] **I6.** The G3 account outside `managed_accounts`: give it a position before I1. **Expect:** I1 leaves it untouched. — Date: ______ NT: ______ Result: ______
- [ ] **I7. The real 16:40, default config.** Restore exactly the A5 config (`"flatten_at": "16:40"`, valid token, `flatten_on_disconnect_seconds: null`) and restart. Before 16:40 New York, open a Sim101 position (a Fenrir bracket via `post bracket`, plus a manual 1-lot on another instrument with no stop), then stop Fenrir (`docker compose stop`) and every stream client. Market data running (Simulated Data Feed, or the live feed: CME index futures trade until 17:00 ET). **Expect:** between 16:40:00 and 16:40:15 New York, alert `Fenrir: 16:40 New York flatten: flattening and cancelling on Sim101`, then `… flat on Sim101` per instrument; Sim101 flat, no working orders. Record the PC time zone and the alert and fill times. — Date: ______ NT: ______ Result: ______

## Sign-off

SC6 passes when I1, I2, I4 and I7 pass. The add-on is done (issue #68) when every line above has a date, a NinjaTrader version and a result.

- Signed off by: ______ · Date: ______ · NinjaTrader version: ______
