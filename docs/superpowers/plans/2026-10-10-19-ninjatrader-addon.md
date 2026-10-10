# NinjaTrader Add-on Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A NinjaScript add-on that exposes NinjaTrader 8 accounts, positions, working orders, bracket entry, flatten and cancel over HTTP + WebSocket with a shared token, dedupes client order IDs, streams account, position, order and fill events with a heartbeat, and flattens everything at 16:40 America/New_York on its own.

**Architecture:** Three thin C# files inside NinjaTrader's `bin\Custom\AddOns\Fenrir\`: `FenrirAddOn.cs` (NinjaTrader lifecycle, config, 16:40 timer), `FenrirServer.cs` (`HttpListener` routing, token check, WebSocket fan-out through one bounded queue and one sender thread so NinjaTrader's threads never wait on the network), `FenrirBroker.cs` (calls into NinjaTrader's `Account` API and turns its events into JSON dictionaries). No market data and no trading decisions. Brackets are a market entry; when the entry fills, the add-on places an OCO stop and target for the filled quantity. The client ID travels in the NinjaTrader order name (`F|<client_id>|E/S/T`), which also lets the add-on rebuild its dedupe set after a restart.

**Tech Stack:** C# (NinjaScript, .NET Framework 4.8, C# 5 syntax to be safe with NinjaTrader's compiler), `System.Net.HttpListener`, `System.Net.WebSockets`, `System.Web.Script.Serialization.JavaScriptSerializer`, NinjaTrader 8 `NinjaTrader.Cbi` Account API, PowerShell.

**Spec:** docs/superpowers/specs/2026-10-10-fenrir-desk-v1-design.md (§9, §8 Failure handling, §11, §12 Add-on)

**Index:** docs/superpowers/plans/README.md (contract: "Plan 19/20 — add-on HTTP API")

**Testing note:** NinjaScript compiles and runs only inside NinjaTrader 8 on Windows. Nothing here can be unit tested on macOS. Every task ends with a manual verification on a NinjaTrader **Sim101** account, run from the Windows machine (or the Mac, against the VM's IP). Where this plan names a NinjaTrader API call it is not certain of, the task has a step to confirm it against NinjaTrader's help guide (<https://ninjatrader.com/support/helpGuides/nt8/>) before compiling.

## Global Constraints

- The add-on handles orders, accounts and positions only: **no market data and no trading decisions** (spec §9).
- Every request carries the shared token (`X-Fenrir-Token` header; `?token=` on the WebSocket). Wrong or missing token → HTTP 401 / socket refused. The add-on refuses to start without a token of at least 16 characters.
- A client order ID already seen is ignored and answered with `{"accepted": false, "duplicate": true}` (spec §9).
- Flatten and cancel all at 16:40 America/New_York, whether or not Python is connected (spec §8, §9).
- Every position opened through the add-on is protected by a broker-side OCO stop and target.
- HTTP API shape is exactly the index contract (`/accounts`, `/positions`, `/orders`, `/bracket`, `/flatten`, `/cancel`, `/flatten-all`, `/stream`). Fill `qty` is signed (+ buy, − sell); `/orders` returns working orders only; `role` is `"entry" | "stop" | "target" | "other"` (`"other"` = an order not placed by Fenrir).
- Instrument names are NinjaTrader's (`"NQ 12-26"`). Timestamps are UTC ISO 8601 with milliseconds and `Z`.
- Kept as thin as NinjaScript allows: forward to NinjaTrader's API and nothing else (spec §9).
- Never let an exception escape a timer callback or NinjaTrader event handler (it would crash NinjaTrader).
- Distributed as an importable NinjaScript zip built from the source in `addon/` (spec §9).
- Commits: Conventional Commits, scope `addon`.

## Review Focus

1. **NinjaTrader restarts while Python retries a bracket** → the retry must come back `duplicate`, not open a second position. The dedupe set is seeded from the names of orders still on the account. Check: Task 3 Step 7.
2. **The stop price is already through the market when the exits are placed** (fast move during the entry fill) → NinjaTrader rejects the stop; the add-on must flatten that instrument at once rather than leave the position unprotected. Check: Task 3 Step 8.
3. **A malformed request or a bug in an event handler** → a 500 for that request (or a log line), never a crashed NinjaTrader. Check: Task 1 Step 9, Task 3 Step 9.
4. **A stalled or dead WebSocket client** → NinjaTrader's order and execution events keep flowing; the stalled client is dropped. Check: Task 4 Step 6.
5. **The Windows machine is not on Eastern time, or DST changes** → the auto-flatten still fires at 16:40 New York time. Check: Task 5 Step 4.

---

### Task 1: Skeleton — config, HTTP server, token, `GET /accounts`

**Files:**
- Create: `addon/Fenrir/FenrirAddOn.cs`
- Create: `addon/Fenrir/FenrirServer.cs`
- Create: `addon/Fenrir/FenrirBroker.cs`
- Create: `addon/fenrir-addon.example.json`
- Create: `addon/install.ps1`
- Create: `addon/INSTALL.md`
- Create: `addon/CLAUDE.md`
- Modify: `CLAUDE.md` (Directory Map table)

**Interfaces:**
- Consumes: nothing from Python.
- Produces: `GET /accounts` → `[{id, name, is_sim, balance, realized_pnl_today, unrealized_pnl}]`; config file `Documents\NinjaTrader 8\fenrir-addon.json` = `{"port": 8123, "token": "<same as FENRIR_NT_TOKEN>", "flatten_at": "16:40"}`.

- [ ] **Step 1: Confirm the NinjaTrader APIs this task uses**

In the NinjaTrader 8 help guide, confirm and note any differences before writing code:
- `AddOnBase.OnStateChange()` reaches `State.SetDefaults`, then `State.Active` when NinjaTrader starts the add-on, and `State.Terminated` on shutdown. (If add-ons never reach `State.Active`, start the server from `State.Configure` instead.)
- `Account.All` (lockable collection), `Account.Name`, `Account.DisplayName`, `Account.Provider` with `Provider.Simulator` for Sim accounts.
- `Account.Get(AccountItem.CashValue, Currency.UsDollar)`, `AccountItem.RealizedProfitLoss`, `AccountItem.UnrealizedProfitLoss`.
- `NinjaTrader.Core.Globals.UserDataDir` (`Documents\NinjaTrader 8\`), `NinjaTrader.Code.Output.Process(string, PrintTo.OutputTab1)`.

- [ ] **Step 2: Write `FenrirAddOn.cs`**

`addon/Fenrir/FenrirAddOn.cs`:

```csharp
#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using NinjaTrader.Cbi;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class FenrirConfig
    {
        public int Port = 8123;
        public string Token = "";
        public string FlattenAt = "16:40"; // America/New_York; configurable only so the auto-flatten can be tested

        public static FenrirConfig Load(string path)
        {
            FenrirConfig config = new FenrirConfig();
            if (!File.Exists(path)) return config;
            Dictionary<string, object> raw =
                new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            object v;
            if (raw.TryGetValue("port", out v)) config.Port = Convert.ToInt32(v);
            if (raw.TryGetValue("token", out v)) config.Token = Convert.ToString(v);
            if (raw.TryGetValue("flatten_at", out v)) config.FlattenAt = Convert.ToString(v);
            return config;
        }
    }

    public class FenrirAddOn : AddOnBase
    {
        private FenrirConfig config;
        private FenrirBroker broker;
        private FenrirServer server;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "Fenrir";
                Description = "Fenrir Desk bridge: accounts, positions, orders over HTTP + WebSocket. No market data, no decisions.";
            }
            else if (State == State.Active)
            {
                Start();
            }
            else if (State == State.Terminated)
            {
                Shutdown();
            }
        }

        public static void Log(string message)
        {
            NinjaTrader.Code.Output.Process("Fenrir: " + message, PrintTo.OutputTab1);
        }

        private void Start()
        {
            try
            {
                config = FenrirConfig.Load(Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "fenrir-addon.json"));
                if (config.Token.Length < 16)
                {
                    Log("set a token of at least 16 characters in fenrir-addon.json; add-on not started");
                    return;
                }
                broker = new FenrirBroker();
                broker.AttachAll();
                server = new FenrirServer(config, broker);
                server.Start();
                Log(string.Format("listening on port {0}", config.Port));
            }
            catch (Exception e)
            {
                Log("failed to start: " + e);
            }
        }

        private void Shutdown()
        {
            try
            {
                if (server != null) server.Stop();
                if (broker != null) broker.DetachAll();
            }
            catch (Exception e)
            {
                Log("shutdown: " + e.Message);
            }
        }
    }
}
```

- [ ] **Step 3: Write `FenrirBroker.cs` (accounts only for now)**

`addon/Fenrir/FenrirBroker.cs`:

```csharp
#region Using declarations
using System;
using System.Collections.Generic;
using System.Linq;
using NinjaTrader.Cbi;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    // Forwards to NinjaTrader's Account API and turns results and events into JSON-ready dictionaries.
    public class FenrirBroker
    {
        private readonly object gate = new object();
        private readonly HashSet<Account> attached = new HashSet<Account>();

        public Action<Dictionary<string, object>> Publish = delegate { };

        public void AttachAll()
        {
            List<Account> accounts;
            lock (Account.All) accounts = Account.All.ToList();
            foreach (Account a in accounts) Attach(a);
        }

        public void DetachAll()
        {
            lock (gate) attached.Clear();
        }

        private void Attach(Account a)
        {
            lock (gate)
            {
                if (!attached.Add(a)) return;
            }
        }

        public Account Find(string id)
        {
            AttachAll(); // accounts whose connection came up after start
            lock (Account.All) return Account.All.FirstOrDefault(a => a.Name == id);
        }

        public List<object> Accounts()
        {
            List<Account> accounts;
            lock (Account.All) accounts = Account.All.ToList();
            return accounts.Select(a => (object)AccountJson(a)).ToList();
        }

        public static Dictionary<string, object> AccountJson(Account a)
        {
            return new Dictionary<string, object>
            {
                { "id", a.Name },
                { "name", a.DisplayName },
                { "is_sim", a.Provider == Provider.Simulator },
                { "balance", a.Get(AccountItem.CashValue, Currency.UsDollar) },
                { "realized_pnl_today", a.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar) },
                { "unrealized_pnl", a.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar) },
            };
        }
    }
}
```

- [ ] **Step 4: Write `FenrirServer.cs` (HTTP + token, GET routes)**

`addon/Fenrir/FenrirServer.cs`:

```csharp
#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class FenrirServer
    {
        private readonly FenrirConfig config;
        private readonly FenrirBroker broker;
        private readonly HttpListener listener = new HttpListener();
        private readonly JavaScriptSerializer json = new JavaScriptSerializer();

        public FenrirServer(FenrirConfig config, FenrirBroker broker)
        {
            this.config = config;
            this.broker = broker;
        }

        public void Start()
        {
            listener.Prefixes.Add(string.Format("http://+:{0}/", config.Port));
            listener.Start();
            Task.Run(() => AcceptLoop());
        }

        public void Stop()
        {
            listener.Stop();
            listener.Close();
        }

        private async Task AcceptLoop()
        {
            while (listener.IsListening)
            {
                HttpListenerContext ctx;
                try { ctx = await listener.GetContextAsync(); }
                catch { return; } // listener stopped
                HttpListenerContext c = ctx;
                Task.Run(() => Handle(c));
            }
        }

        private bool TokenOk(string given)
        {
            // constant-time compare
            if (given == null || given.Length != config.Token.Length) return false;
            int diff = 0;
            for (int i = 0; i < given.Length; i++) diff |= given[i] ^ config.Token[i];
            return diff == 0;
        }

        private async Task Handle(HttpListenerContext ctx)
        {
            try
            {
                HttpListenerRequest req = ctx.Request;
                if (!TokenOk(req.Headers["X-Fenrir-Token"]))
                {
                    Reply(ctx, 401, Error("bad token"));
                    return;
                }
                object result = Route(req.HttpMethod, req.Url.AbsolutePath, req);
                if (result == null) Reply(ctx, 404, Error("not found"));
                else Reply(ctx, 200, result);
            }
            catch (Exception e)
            {
                FenrirAddOn.Log("request failed: " + e.Message);
                try { Reply(ctx, 500, Error(e.Message)); } catch { }
            }
            await Task.FromResult(0);
        }

        private object Route(string method, string path, HttpListenerRequest req)
        {
            if (method == "GET" && path == "/accounts") return broker.Accounts();
            return null;
        }

        private static Dictionary<string, object> Error(string message)
        {
            return new Dictionary<string, object> { { "error", message } };
        }

        private void Reply(HttpListenerContext ctx, int status, object body)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json.Serialize(body));
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.Close();
        }
    }
}
```

- [ ] **Step 5: Add the config example, install script and install guide**

`addon/fenrir-addon.example.json`:

```json
{ "port": 8123, "token": "", "flatten_at": "16:40" }
```

`addon/install.ps1`:

```powershell
# Copies the add-on source into NinjaTrader's custom folder. Compile it in NinjaTrader afterwards (F5).
$dest = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8\bin\Custom\AddOns\Fenrir'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot 'Fenrir\*.cs') -Destination $dest -Force
$config = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NinjaTrader 8\fenrir-addon.json'
if (-not (Test-Path $config)) { Copy-Item (Join-Path $PSScriptRoot 'fenrir-addon.example.json') $config }
Write-Host "Copied to $dest"
Write-Host "Edit $config and set the token (same value as FENRIR_NT_TOKEN in Fenrir's .env)."
Write-Host "Then in NinjaTrader: New > NinjaScript Editor, press F5 to compile."
```

`addon/INSTALL.md`:

````markdown
# Installing the Fenrir NinjaTrader add-on

Requires NinjaTrader 8 on Windows (the host, or a VM when Fenrir runs on a Mac).

## 1. Install

Either import the release zip (**Tools → Import → NinjaScript Add-On…**, pick `FenrirAddOn.zip`), or install from source:

```powershell
powershell -ExecutionPolicy Bypass -File addon\install.ps1
```

then open **New → NinjaScript Editor**, right-click → **References…**, add
`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll` if it is not listed, and press **F5** to compile.

## 2. Token

Generate one token and use it in both places:

```powershell
[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]])
```

- `Documents\NinjaTrader 8\fenrir-addon.json` → `"token"`
- Fenrir's `.env` → `FENRIR_NT_TOKEN`

Restart NinjaTrader. The NinjaScript Output window shows `Fenrir: listening on port 8123`.

## 3. Allow the port (run PowerShell as Administrator)

```powershell
netsh http add urlacl url=http://+:8123/ user=Everyone
netsh advfirewall firewall add rule name="Fenrir add-on 8123" dir=in action=allow protocol=TCP localport=8123 remoteip=localsubnet
```

- **Fenrir in Docker on the same Windows machine:** `FENRIR_NT_URL=http://host.docker.internal:8123`. If requests time out, the WSL/Docker virtual network is not in `localsubnet`: run `ipconfig`, find the `vEthernet (WSL)` adapter's subnet, and use it as `remoteip=` instead (for example `remoteip=172.16.0.0/12`).
- **Fenrir on a Mac, NinjaTrader in a VM:** run `ipconfig` in the VM, take the IPv4 address of its network adapter, and set `FENRIR_NT_URL=http://<that IP>:8123`. Use bridged or shared networking so the Mac and VM share a subnet.

## 4. Check

From the machine running Fenrir:

```bash
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" http://<host>:8123/accounts
```

Expected: a JSON list including `Sim101`.

## 5. Build the release zip

In NinjaTrader: **Tools → Export → NinjaScript Add-On…**, select the Fenrir files, export as `FenrirAddOn.zip`. Attach it to a GitHub release; do not commit it.
````

- [ ] **Step 6: Add the directory context file and map row**

`addon/CLAUDE.md`:

```markdown
# addon/

The NinjaTrader 8 add-on (C# NinjaScript): HTTP + WebSocket server for accounts, positions, orders, fills, heartbeat, and the independent 16:40 ET flatten.

- Entry points: `Fenrir/FenrirAddOn.cs` (lifecycle, config, 16:40 timer), `Fenrir/FenrirServer.cs` (routes, token, WebSocket fan-out), `Fenrir/FenrirBroker.cs` (NinjaTrader Account API calls and event JSON).
- Pure vs shell: all shell; keep it thin. No market data, no trading decisions.
- Not here: the Python client and reconciliation (`src/fenrir/bridge/`); when to trade (`src/fenrir/live/`).
- Compiles only inside NinjaTrader on Windows; C# 5 syntax (no `$""`, `?.`, `=>` members). The API contract is in `docs/superpowers/plans/README.md` ("Plan 19/20").
- Install: `INSTALL.md`. Tests: manual, `CHECKLIST.md` on Sim101.
```

In root `CLAUDE.md`, add this row to the `## 6. Directory Map` table:

```markdown
| `addon/` | NinjaTrader 8 add-on (C#): order, account and position bridge over HTTP + WebSocket, 16:40 ET auto-flatten. |
```

- [ ] **Step 7: Install and compile on Windows**

Run in PowerShell: `powershell -ExecutionPolicy Bypass -File addon\install.ps1`, set the token in `fenrir-addon.json`, add the `System.Web.Extensions` reference, press F5 in the NinjaScript Editor, restart NinjaTrader, run the two `netsh` commands from `INSTALL.md`.
Expected: compile succeeds with no errors; the Output window (New → NinjaScript Output) shows `Fenrir: listening on port 8123`.

- [ ] **Step 8: Verify accounts and token rejection**

Run (from the Fenrir machine):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<host>:8123/accounts
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Fenrir-Token: wrong" http://<host>:8123/accounts
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" http://<host>:8123/accounts
```

Expected: `401`, `401`, then a JSON list containing `{"id":"Sim101",...,"is_sim":true,...}`.

- [ ] **Step 9: Verify an unknown path does not crash NinjaTrader**

Run: `curl -s -w "\n%{http_code}\n" -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" http://<host>:8123/nope`
Expected: `{"error":"not found"}` and `404`; NinjaTrader still running.

- [ ] **Step 10: Commit**

```bash
git add addon/ CLAUDE.md
git commit -m "feat(addon): add ninjatrader add-on skeleton with token-checked accounts endpoint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Positions and working orders

**Files:**
- Modify: `addon/Fenrir/FenrirBroker.cs`
- Modify: `addon/Fenrir/FenrirServer.cs`

**Interfaces:**
- Produces: `GET /positions?account=ID` → `[{account, instrument, qty, avg_price}]` (signed qty, flat positions omitted); `GET /orders?account=ID` → `[{account, order_id, client_id, instrument, role, action, qty, price, state}]` (working orders only). Unknown account → `[]`.

- [ ] **Step 1: Confirm the NinjaTrader APIs**

Confirm in the help guide: `Account.Positions` (items: `Instrument.FullName`, `MarketPosition` Long/Short/Flat, `Quantity`, `AveragePrice`); `Account.Orders` (items: `OrderId`, `Name`, `Instrument`, `OrderAction` Buy/Sell/BuyToCover/SellShort, `Quantity`, `LimitPrice`, `StopPrice`, `OrderState`); static `Order.IsTerminalState(OrderState)`; whether `Account.Positions`/`Account.Orders` should be locked while enumerating.

- [ ] **Step 2: Add order-name helpers and JSON builders to `FenrirBroker.cs`**

Inside `FenrirBroker`, add:

```csharp
        // Order names carry the client id: "F|<client_id>|E" (entry), "|S" (stop), "|T" (target).
        public static string Name(string clientId, string role) { return "F|" + clientId + "|" + role; }

        public static string ClientId(string orderName)
        {
            if (orderName == null || !orderName.StartsWith("F|")) return null;
            int last = orderName.LastIndexOf('|');
            return last > 2 ? orderName.Substring(2, last - 2) : null;
        }

        public static string Role(string orderName)
        {
            if (ClientId(orderName) == null) return "other";
            switch (orderName[orderName.Length - 1])
            {
                case 'E': return "entry";
                case 'S': return "stop";
                case 'T': return "target";
                default: return "other";
            }
        }

        private static List<T> Snapshot<T>(IEnumerable<T> items)
        {
            lock (items) return items.ToList(); // NinjaTrader mutates these collections on its own threads
        }

        public static Dictionary<string, object> PositionJson(Account a, Position p)
        {
            int qty = p.MarketPosition == MarketPosition.Short ? -p.Quantity
                    : p.MarketPosition == MarketPosition.Long ? p.Quantity : 0;
            return new Dictionary<string, object>
            {
                { "account", a.Name }, { "instrument", p.Instrument.FullName },
                { "qty", qty }, { "avg_price", p.AveragePrice },
            };
        }

        public static Dictionary<string, object> OrderJson(Account a, Order o)
        {
            bool buy = o.OrderAction == OrderAction.Buy || o.OrderAction == OrderAction.BuyToCover;
            return new Dictionary<string, object>
            {
                { "account", a.Name }, { "order_id", o.OrderId }, { "client_id", ClientId(o.Name) ?? "" },
                { "instrument", o.Instrument.FullName }, { "role", Role(o.Name) },
                { "action", buy ? "buy" : "sell" }, { "qty", o.Quantity },
                { "price", o.LimitPrice > 0 ? o.LimitPrice : o.StopPrice }, { "state", o.OrderState.ToString() },
            };
        }

        public List<object> Positions(string accountId)
        {
            Account a = Find(accountId);
            if (a == null) return new List<object>();
            return Snapshot(a.Positions).Where(p => p.MarketPosition != MarketPosition.Flat)
                .Select(p => (object)PositionJson(a, p)).ToList();
        }

        public List<object> Orders(string accountId)
        {
            Account a = Find(accountId);
            if (a == null) return new List<object>();
            return Snapshot(a.Orders).Where(o => !Order.IsTerminalState(o.OrderState))
                .Select(o => (object)OrderJson(a, o)).ToList();
        }
```

- [ ] **Step 3: Route them in `FenrirServer.cs`**

In `Route`, before `return null;`, add:

```csharp
            if (method == "GET" && path == "/positions") return broker.Positions(req.QueryString["account"]);
            if (method == "GET" && path == "/orders") return broker.Orders(req.QueryString["account"]);
```

- [ ] **Step 4: Recompile**

Run: `powershell -ExecutionPolicy Bypass -File addon\install.ps1`, then F5 in the NinjaScript Editor.
Expected: compiles with no errors; Output shows `Fenrir: listening on port 8123` after the add-on reloads (restart NinjaTrader if it does not).

- [ ] **Step 5: Verify on Sim101**

In NinjaTrader, open a Chart Trader or SuperDOM on `MNQ 12-26` with account Sim101, buy 1 at market, and place a manual sell limit 100 points above. Then run:

```bash
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" "http://<host>:8123/positions?account=Sim101"
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" "http://<host>:8123/orders?account=Sim101"
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" "http://<host>:8123/positions?account=Nope"
```

Expected: `[{"account":"Sim101","instrument":"MNQ 12-26","qty":1,"avg_price":...}]`; one order with `"role":"other"`, `"action":"sell"`, `"state":"Working"`; `[]`. Close the position and cancel the order by hand afterwards.

- [ ] **Step 6: Commit**

```bash
git add addon/Fenrir/FenrirBroker.cs addon/Fenrir/FenrirServer.cs
git commit -m "feat(addon): list positions and working orders

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Brackets, flatten, cancel, flatten-all, dedupe

**Files:**
- Modify: `addon/Fenrir/FenrirBroker.cs`
- Modify: `addon/Fenrir/FenrirServer.cs`

**Interfaces:**
- Produces: `POST /bracket {client_id, account, instrument, side, qty, stop, target}`, `POST /flatten {client_id, account}`, `POST /cancel {client_id, account, order_id}`, `POST /flatten-all {client_id}` → `{accepted, duplicate, error}`. `/flatten-all` flattens and cancels on every account.

- [ ] **Step 1: Confirm the NinjaTrader APIs**

Confirm in the help guide:
- `Account.CreateOrder(Instrument, OrderAction, OrderType, OrderEntry, TimeInForce, int quantity, double limitPrice, double stopPrice, string oco, string name, DateTime gtd, CustomOrder)` and `Account.Submit(IEnumerable<Order>)`, `Account.Cancel(IEnumerable<Order>)`, `Account.Flatten(ICollection<Instrument>)` (cancels working orders and closes positions for those instruments).
- `Instrument.GetInstrument("NQ 12-26")` returns `null` for unknown names.
- `Account.OrderUpdate` event with `OrderEventArgs` (`Order`, `OrderState`, `Filled`); `Order.Account`, `Order.Filled`.
- The maximum length of an order `Name` (client IDs from Fenrir are at most 24 characters, so names are at most 28).
- `OrderType.StopMarket`, `OrderType.Limit`, `OrderType.Market`, `OrderEntry.Automated`, `TimeInForce.Day`, `TimeInForce.Gtc`, `NinjaTrader.Core.Globals.MaxDate`.

- [ ] **Step 2: Add dedupe, order placement and exits to `FenrirBroker.cs`**

Inside `FenrirBroker`, add fields:

```csharp
        private readonly HashSet<string> seen = new HashSet<string>();
        private readonly Dictionary<string, double[]> exits = new Dictionary<string, double[]>(); // client id -> {stop, target}
```

Replace `Attach` with:

```csharp
        private void Attach(Account a)
        {
            lock (gate)
            {
                if (!attached.Add(a)) return;
                // orders still on the account after a NinjaTrader restart keep their client ids "seen"
                foreach (Order o in Snapshot(a.Orders))
                {
                    string cid = ClientId(o.Name);
                    if (cid != null) seen.Add(cid);
                }
            }
            a.OrderUpdate += OnOrderUpdate;
        }
```

Replace `DetachAll` with:

```csharp
        public void DetachAll()
        {
            lock (gate)
            {
                foreach (Account a in attached) a.OrderUpdate -= OnOrderUpdate;
                attached.Clear();
            }
        }
```

Add:

```csharp
        private bool Claim(string clientId)
        {
            lock (gate) return seen.Add(clientId);
        }

        private static Dictionary<string, object> Ack(bool accepted, bool duplicate, string error)
        {
            return new Dictionary<string, object> { { "accepted", accepted }, { "duplicate", duplicate }, { "error", error } };
        }

        private static string Str(Dictionary<string, object> body, string key)
        {
            object v;
            return body.TryGetValue(key, out v) && v != null ? Convert.ToString(v) : null;
        }

        public Dictionary<string, object> Bracket(Dictionary<string, object> b)
        {
            string cid = Str(b, "client_id");
            if (string.IsNullOrEmpty(cid)) return Ack(false, false, "client_id required");
            Account account = Find(Str(b, "account"));
            if (account == null) return Ack(false, false, "unknown account");
            Instrument instrument = Instrument.GetInstrument(Str(b, "instrument"));
            if (instrument == null) return Ack(false, false, "unknown instrument");
            string side = Str(b, "side");
            int qty = Convert.ToInt32(b["qty"]);
            double stop = Convert.ToDouble(b["stop"]);
            double target = Convert.ToDouble(b["target"]);
            bool isLong = side == "long";
            if (qty <= 0 || (side != "long" && side != "short") || (isLong ? stop >= target : stop <= target))
                return Ack(false, false, "invalid bracket");
            if (!Claim(cid)) return Ack(false, true, null);
            lock (gate) exits[cid] = new[] { stop, target };
            Order entry = account.CreateOrder(instrument, isLong ? OrderAction.Buy : OrderAction.SellShort,
                OrderType.Market, OrderEntry.Automated, TimeInForce.Day, qty, 0, 0, string.Empty,
                Name(cid, "E"), NinjaTrader.Core.Globals.MaxDate, null);
            account.Submit(new[] { entry });
            return Ack(true, false, null);
        }

        private void OnOrderUpdate(object sender, OrderEventArgs e)
        {
            try
            {
                Order o = e.Order;
                Publish(WithType("order", OrderJson(o.Account, o)));
                string cid = ClientId(o.Name);
                if (cid == null) return;
                string role = Role(o.Name);
                if (role == "entry" && e.OrderState == OrderState.Filled) SubmitExits(o, cid);
                if ((role == "stop" || role == "target") && e.OrderState == OrderState.Rejected)
                {
                    // an unprotected position is never left open
                    o.Account.Flatten(new[] { o.Instrument });
                    FenrirAddOn.Log("exit rejected for " + cid + "; flattened " + o.Instrument.FullName);
                }
            }
            catch (Exception ex)
            {
                FenrirAddOn.Log("order update: " + ex.Message);
            }
        }

        private void SubmitExits(Order entry, string cid)
        {
            double[] px;
            lock (gate)
            {
                if (!exits.TryGetValue(cid, out px)) return; // already placed
                exits.Remove(cid);
            }
            OrderAction exit = entry.OrderAction == OrderAction.Buy ? OrderAction.Sell : OrderAction.BuyToCover;
            string oco = "F|" + cid;
            Order stop = entry.Account.CreateOrder(entry.Instrument, exit, OrderType.StopMarket, OrderEntry.Automated,
                TimeInForce.Gtc, entry.Filled, 0, px[0], oco, Name(cid, "S"), NinjaTrader.Core.Globals.MaxDate, null);
            Order target = entry.Account.CreateOrder(entry.Instrument, exit, OrderType.Limit, OrderEntry.Automated,
                TimeInForce.Gtc, entry.Filled, px[1], 0, oco, Name(cid, "T"), NinjaTrader.Core.Globals.MaxDate, null);
            entry.Account.Submit(new[] { stop, target });
        }

        public bool FlattenAccount(Account a)
        {
            List<Instrument> instruments = new List<Instrument>();
            foreach (Position p in Snapshot(a.Positions))
                if (p.MarketPosition != MarketPosition.Flat && !instruments.Contains(p.Instrument)) instruments.Add(p.Instrument);
            foreach (Order o in Snapshot(a.Orders))
                if (!Order.IsTerminalState(o.OrderState) && !instruments.Contains(o.Instrument)) instruments.Add(o.Instrument);
            if (instruments.Count == 0) return false;
            a.Flatten(instruments);
            return true;
        }

        public bool FlattenEverything()
        {
            List<Account> accounts;
            lock (Account.All) accounts = Account.All.ToList();
            bool any = false;
            foreach (Account a in accounts) any |= FlattenAccount(a);
            return any;
        }

        public Dictionary<string, object> Flatten(Dictionary<string, object> b)
        {
            string cid = Str(b, "client_id");
            if (string.IsNullOrEmpty(cid)) return Ack(false, false, "client_id required");
            Account account = Find(Str(b, "account"));
            if (account == null) return Ack(false, false, "unknown account");
            if (!Claim(cid)) return Ack(false, true, null);
            FlattenAccount(account);
            return Ack(true, false, null);
        }

        public Dictionary<string, object> Cancel(Dictionary<string, object> b)
        {
            string cid = Str(b, "client_id");
            if (string.IsNullOrEmpty(cid)) return Ack(false, false, "client_id required");
            Account account = Find(Str(b, "account"));
            if (account == null) return Ack(false, false, "unknown account");
            string orderId = Str(b, "order_id");
            Order order = Snapshot(account.Orders).FirstOrDefault(o => o.OrderId == orderId);
            if (order == null) return Ack(false, false, "unknown order");
            if (!Claim(cid)) return Ack(false, true, null);
            account.Cancel(new[] { order });
            return Ack(true, false, null);
        }

        public Dictionary<string, object> FlattenAll(Dictionary<string, object> b)
        {
            string cid = Str(b, "client_id");
            if (string.IsNullOrEmpty(cid)) return Ack(false, false, "client_id required");
            if (!Claim(cid)) return Ack(false, true, null);
            FlattenEverything();
            return Ack(true, false, null);
        }

        public static Dictionary<string, object> WithType(string type, Dictionary<string, object> body)
        {
            Dictionary<string, object> d = new Dictionary<string, object>(body);
            d["type"] = type;
            return d;
        }
```

- [ ] **Step 3: Route POSTs in `FenrirServer.cs`**

In `Route`, before `return null;`, add:

```csharp
            if (method == "POST")
            {
                string text;
                using (StreamReader r = new StreamReader(req.InputStream, Encoding.UTF8)) text = r.ReadToEnd();
                Dictionary<string, object> body = json.Deserialize<Dictionary<string, object>>(text);
                if (path == "/bracket") return broker.Bracket(body);
                if (path == "/flatten") return broker.Flatten(body);
                if (path == "/cancel") return broker.Cancel(body);
                if (path == "/flatten-all") return broker.FlattenAll(body);
            }
```

- [ ] **Step 4: Recompile**

Run: `powershell -ExecutionPolicy Bypass -File addon\install.ps1`, then F5.
Expected: no compile errors.

- [ ] **Step 5: Verify a bracket on Sim101**

With the market open (or Sim101 on the Playback/Simulated Data Feed connection), read the current MNQ price `P` from a chart, then:

```bash
post() { curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" -H "Content-Type: application/json" -d "$2" "http://<host>:8123/$1"; echo; }
post bracket '{"client_id":"t-1","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":P_MINUS_20,"target":P_PLUS_40}'
curl -s -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" "http://<host>:8123/orders?account=Sim101"; echo
```

(Replace `P_MINUS_20` / `P_PLUS_40` with numbers.)
Expected: `{"accepted":true,"duplicate":false,"error":null}`; `/orders` lists two working orders for `MNQ 12-26`, one `"role":"stop"` and one `"role":"target"`, both `"client_id":"t-1"`, `"qty":1`, `"action":"sell"`. NinjaTrader's Orders tab shows them linked by OCO.

- [ ] **Step 6: Verify dedupe, flatten, cancel, flatten-all**

```bash
post bracket '{"client_id":"t-1","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":1,"target":99999}'
post flatten '{"client_id":"t-2","account":"Sim101"}'
post flatten '{"client_id":"t-2","account":"Sim101"}'
post bracket '{"client_id":"t-3","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":P_MINUS_20,"target":P_PLUS_40}'
# take the stop's order_id from /orders, then:
post cancel '{"client_id":"t-4","account":"Sim101","order_id":"<stop order id>"}'
post flatten-all '{"client_id":"t-5"}'
post bracket '{"client_id":"t-6","account":"Nope","instrument":"MNQ 12-26","side":"long","qty":1,"stop":1,"target":2}'
post bracket '{"client_id":"t-7","account":"Sim101","instrument":"MNQ 12-26","side":"long","qty":1,"stop":2,"target":1}'
```

Expected, in order: `duplicate: true` (no new order); `accepted: true` and Sim101 flat with no working orders; `duplicate: true`; a new protected position; `accepted: true` and the stop disappears from `/orders`; `accepted: true` and every account flat with no working orders; `error: "unknown account"`; `error: "invalid bracket"`.

- [ ] **Step 7: Verify dedupe survives a NinjaTrader restart** (Review Focus 1)

Place bracket `t-8` as in Step 5. Restart NinjaTrader (the stop and target stay on Sim101). Then resend exactly the same `t-8` request.
Expected: `{"accepted":false,"duplicate":true,...}` and still one position of 1. Flatten afterwards with `t-9`.

- [ ] **Step 8: Verify a rejected exit flattens** (Review Focus 2)

Send a long bracket whose `stop` is **above** the current price but below `target`, e.g. `stop: P+5`, `target: P+40`, `client_id: t-10`.
Expected: the entry fills, NinjaTrader rejects the sell stop (it is above the market), the Output window shows `Fenrir: exit rejected for t-10; flattened MNQ 12-26`, and Sim101 ends flat with no working orders.

- [ ] **Step 9: Verify malformed JSON does not crash NinjaTrader** (Review Focus 3)

Run: `curl -s -w "\n%{http_code}\n" -H "X-Fenrir-Token: $FENRIR_NT_TOKEN" -d '{not json' http://<host>:8123/bracket`
Expected: a JSON `error` body and `500`; NinjaTrader keeps running.

- [ ] **Step 10: Commit**

```bash
git add addon/Fenrir/FenrirBroker.cs addon/Fenrir/FenrirServer.cs
git commit -m "feat(addon): place oco brackets, flatten and cancel with client id dedupe

Exits are placed when the entry fills; a rejected exit flattens the
instrument so no position is left unprotected. Client ids of orders
still on the account survive a NinjaTrader restart.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Event stream and heartbeat

**Files:**
- Modify: `addon/Fenrir/FenrirBroker.cs`
- Modify: `addon/Fenrir/FenrirServer.cs`

**Interfaces:**
- Produces: `WS /stream?token=` sending `{type:"heartbeat", ts}` every 2 s; `{type:"account", ...account}` every 2 s per account; `{type:"position", ...position}` and `{type:"order", ...order}` on change; `{type:"fill", account, client_id, order_id, instrument, qty (signed), price, commission, ts}` on each execution.

- [ ] **Step 1: Confirm the NinjaTrader APIs**

Confirm in the help guide: `Account.ExecutionUpdate` with `ExecutionEventArgs.Execution` (`Account`, `Order`, `Instrument`, `MarketPosition`, `Quantity`, `Price`, `Commission`, `Time` in local PC time); `Account.PositionUpdate` with `PositionEventArgs.Position`. Confirm that `HttpListenerContext.AcceptWebSocketAsync` is available (Windows 8+ / Server 2012+) and that the resulting `WebSocket` answers pings automatically.

- [ ] **Step 2: Publish executions and positions from `FenrirBroker.cs`**

Add `using System.Globalization;` to the usings. In `Attach`, after `a.OrderUpdate += OnOrderUpdate;` add:

```csharp
            a.ExecutionUpdate += OnExecutionUpdate;
            a.PositionUpdate += OnPositionUpdate;
```

Replace `DetachAll` with:

```csharp
        public void DetachAll()
        {
            lock (gate)
            {
                foreach (Account a in attached)
                {
                    a.OrderUpdate -= OnOrderUpdate;
                    a.ExecutionUpdate -= OnExecutionUpdate;
                    a.PositionUpdate -= OnPositionUpdate;
                }
                attached.Clear();
            }
        }
```

Add:

```csharp
        public static string Utc(DateTime local)
        {
            return local.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
        }

        private void OnExecutionUpdate(object sender, ExecutionEventArgs e)
        {
            try
            {
                Execution x = e.Execution;
                int qty = x.MarketPosition == MarketPosition.Long ? x.Quantity : -x.Quantity;
                Publish(new Dictionary<string, object>
                {
                    { "type", "fill" }, { "account", x.Account.Name },
                    { "client_id", (x.Order != null ? ClientId(x.Order.Name) : null) ?? "" },
                    { "order_id", x.Order != null ? x.Order.OrderId : "" },
                    { "instrument", x.Instrument.FullName }, { "qty", qty }, { "price", x.Price },
                    { "commission", x.Commission }, { "ts", Utc(x.Time) },
                });
            }
            catch (Exception ex)
            {
                FenrirAddOn.Log("execution update: " + ex.Message);
            }
        }

        private void OnPositionUpdate(object sender, PositionEventArgs e)
        {
            try
            {
                Publish(WithType("position", PositionJson(e.Position.Account, e.Position)));
            }
            catch (Exception ex)
            {
                FenrirAddOn.Log("position update: " + ex.Message);
            }
        }
```

- [ ] **Step 3: Add the WebSocket fan-out to `FenrirServer.cs`**

Add usings: `System.Collections.Concurrent`, `System.Linq`, `System.Net.WebSockets`, `System.Threading`.

Add fields:

```csharp
        private readonly List<WebSocket> sockets = new List<WebSocket>();
        // bounded: if nobody drains it, NinjaTrader's event threads drop messages instead of waiting
        private readonly BlockingCollection<string> outbox = new BlockingCollection<string>(1000);
        private Timer heartbeat;
```

Replace `Start` and `Stop` with:

```csharp
        public void Start()
        {
            listener.Prefixes.Add(string.Format("http://+:{0}/", config.Port));
            listener.Start();
            broker.Publish = Enqueue;
            Task.Run(() => AcceptLoop());
            Thread sender = new Thread(SendLoop);
            sender.IsBackground = true;
            sender.Start();
            heartbeat = new Timer(_ => Beat(), null, 0, 2000);
        }

        public void Stop()
        {
            if (heartbeat != null) heartbeat.Dispose();
            outbox.CompleteAdding();
            listener.Stop();
            listener.Close();
            lock (sockets) foreach (WebSocket ws in sockets) ws.Abort();
        }

        private void Enqueue(Dictionary<string, object> msg)
        {
            if (!outbox.IsAddingCompleted) outbox.TryAdd(json.Serialize(msg)); // full: dropped; Python reconciles
        }

        private void Beat()
        {
            try
            {
                broker.AttachAll();
                Enqueue(new Dictionary<string, object> { { "type", "heartbeat" }, { "ts", FenrirBroker.Utc(DateTime.Now) } });
                foreach (object a in broker.Accounts())
                    Enqueue(FenrirBroker.WithType("account", (Dictionary<string, object>)a));
            }
            catch (Exception e)
            {
                FenrirAddOn.Log("heartbeat: " + e.Message);
            }
        }

        private void SendLoop()
        {
            foreach (string text in outbox.GetConsumingEnumerable())
            {
                ArraySegment<byte> bytes = new ArraySegment<byte>(Encoding.UTF8.GetBytes(text));
                WebSocket[] targets;
                lock (sockets) targets = sockets.ToArray();
                foreach (WebSocket ws in targets)
                {
                    try
                    {
                        if (!ws.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None).Wait(2000))
                            ws.Abort(); // stalled client: drop it rather than hold everyone up
                    }
                    catch
                    {
                        ws.Abort();
                    }
                }
            }
        }

        private async Task HandleSocket(HttpListenerContext ctx)
        {
            if (!TokenOk(ctx.Request.QueryString["token"]))
            {
                ctx.Response.StatusCode = 401;
                ctx.Response.Close();
                return;
            }
            WebSocket ws = (await ctx.AcceptWebSocketAsync(null)).WebSocket;
            lock (sockets) sockets.Add(ws);
            try
            {
                byte[] buffer = new byte[1024];
                while (ws.State == WebSocketState.Open)
                {
                    WebSocketReceiveResult r = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), CancellationToken.None);
                    if (r.MessageType == WebSocketMessageType.Close) break;
                }
            }
            catch
            {
                // client went away
            }
            finally
            {
                lock (sockets) sockets.Remove(ws);
                ws.Dispose();
            }
        }
```

In `Handle`, as the first statement inside `try`, add:

```csharp
                if (ctx.Request.IsWebSocketRequest && ctx.Request.Url.AbsolutePath == "/stream")
                {
                    await HandleSocket(ctx);
                    return;
                }
```

and remove the trailing `await Task.FromResult(0);`.

- [ ] **Step 4: Recompile**

Run: `powershell -ExecutionPolicy Bypass -File addon\install.ps1`, then F5.
Expected: no compile errors.

- [ ] **Step 5: Verify the stream**

From the Fenrir machine:

```bash
uv run --with websockets python -m websockets "ws://<host>:8123/stream?token=$FENRIR_NT_TOKEN"
```

Expected: a `heartbeat` line every 2 s and an `account` line per account every 2 s. Send a bracket (Task 3 Step 5, new `client_id`) from another terminal: `order` lines for the entry, then a `fill` with `"qty":1` and a `ts` ending in `Z`, a `position` line, then `order` lines for the stop and target. Flatten: a `fill` with `"qty":-1`. Leave the stream open 5 minutes: it stays connected. Then try a wrong token: the connection is refused (HTTP 401).

- [ ] **Step 6: Verify a stalled client does not block NinjaTrader** (Review Focus 4)

Start the stream client above, then suspend it with Ctrl+Z (on Windows, pause the process in Resource Monitor). While it is suspended, send and flatten a bracket.
Expected: the bracket and flatten complete in NinjaTrader as normal; within a few seconds the suspended client is dropped (on resume it shows the connection closed). A second, healthy stream client keeps receiving events throughout.

- [ ] **Step 7: Commit**

```bash
git add addon/Fenrir/FenrirBroker.cs addon/Fenrir/FenrirServer.cs
git commit -m "feat(addon): stream heartbeat, account, position, order and fill events

One bounded queue and one sender thread keep NinjaTrader's event
threads off the network and drop stalled clients.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Independent 16:40 ET flatten

**Files:**
- Modify: `addon/Fenrir/FenrirAddOn.cs`

**Interfaces:**
- Produces: between `flatten_at` (default `16:40`) and 20 minutes later, New York time, every 15 s: flatten and cancel all on every account. Logged when it closes something.

- [ ] **Step 1: Confirm the time zone ID**

Confirm `TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time")` exists on the Windows machine (it is the Windows ID for America/New_York, DST included): run `[TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")` in PowerShell.
Expected: prints the Eastern time zone.

- [ ] **Step 2: Add the timer**

In `FenrirAddOn`, add a field:

```csharp
        private Timer flattenTimer;
```

In `Start`, after `server.Start();`, add:

```csharp
                flattenTimer = new Timer(_ => CheckFlatten(), null, 0, 15000);
```

In `Shutdown`, first line inside `try`:

```csharp
                if (flattenTimer != null) flattenTimer.Dispose();
```

Add the method:

```csharp
        private void CheckFlatten()
        {
            try
            {
                DateTime ny = TimeZoneInfo.ConvertTimeBySystemTimeZoneId(DateTime.UtcNow, "Eastern Standard Time");
                TimeSpan at = TimeSpan.Parse(config.FlattenAt);
                // repeat through the window so anything opened late (by anyone) is closed too
                if (ny.TimeOfDay >= at && ny.TimeOfDay < at + TimeSpan.FromMinutes(20) && broker.FlattenEverything())
                    Log(string.Format("{0} New York flatten: closed positions and cancelled orders", config.FlattenAt));
            }
            catch (Exception e)
            {
                Log("auto-flatten failed, retrying in 15 s: " + e.Message);
            }
        }
```

- [ ] **Step 3: Recompile**

Run: `powershell -ExecutionPolicy Bypass -File addon\install.ps1`, then F5.
Expected: no compile errors.

- [ ] **Step 4: Verify with Fenrir stopped** (Review Focus 5)

Set the Windows clock's time zone to something other than Eastern (e.g. Pacific). In `fenrir-addon.json` set `"flatten_at"` to New York time 3 minutes from now, restart NinjaTrader. Stop the Fenrir server (no Python connected). Open a position with a bracket on Sim101 (Task 3 Step 5).
Expected: within 15 s of the configured New York time, Sim101 is flat with no working orders, and the Output window shows `... New York flatten: closed positions and cancelled orders`. Open another position by hand inside the 20-minute window: it is flattened within 15 s.
Afterwards set `"flatten_at": "16:40"`, restore the time zone and restart NinjaTrader.

- [ ] **Step 5: Commit**

```bash
git add addon/Fenrir/FenrirAddOn.cs
git commit -m "feat(addon): flatten and cancel everything at 16:40 new york time on its own

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Release zip and the Sim checklist

**Files:**
- Create: `addon/CHECKLIST.md`
- Modify: `README.md` (one link)

- [ ] **Step 1: Write the checklist** (spec §12 Add-on)

`addon/CHECKLIST.md`:

```markdown
# Add-on checklist (NinjaTrader Sim101)

Run after every change to `addon/` and before arming any account in `sim` mode. Record date, NinjaTrader version and result for each line. Commands are in `INSTALL.md` and plan 19 (`docs/superpowers/plans/2026-10-10-19-ninjatrader-addon.md`).

- [ ] Token rejection: no token and a wrong token both get 401 on `/accounts`; `/stream` with a wrong token is refused.
- [ ] Accounts: `/accounts` lists Sim101 with `is_sim: true`.
- [ ] Bracket: entry fills, then a stop and target (OCO) for the filled quantity appear; filling one cancels the other.
- [ ] Rejected exit: a stop already through the market → the instrument is flattened at once.
- [ ] Flatten: account flat, no working orders.
- [ ] Cancel: the named order is cancelled; others stay.
- [ ] Flatten-all: every account flat, no working orders.
- [ ] Dedupe: resending any client_id returns `duplicate: true` and places nothing; still true after a NinjaTrader restart while the bracket is working.
- [ ] Stream: heartbeat every 2 s; fill `qty` signed; `ts` ends in `Z`; a stalled client is dropped without delaying orders.
- [ ] 16:40 flatten: with Fenrir stopped and the PC not on Eastern time, positions are closed and orders cancelled at the configured New York time.
- [ ] Malformed request: 500 with an error body; NinjaTrader keeps running.
```

- [ ] **Step 2: Run the whole checklist on Sim101**

Work through every line of `addon/CHECKLIST.md`, using the commands from Tasks 1–5.
Expected: every line passes. Fix and recompile on any failure before continuing.

- [ ] **Step 3: Export the release zip**

In NinjaTrader: Tools → Export → NinjaScript Add-On…, select `FenrirAddOn`, `FenrirServer`, `FenrirBroker` (they appear under AddOns), name it `FenrirAddOn.zip`. Then on a second NinjaTrader install (or after removing `bin\Custom\AddOns\Fenrir`), Tools → Import → NinjaScript Add-On… → `FenrirAddOn.zip`.
Expected: import succeeds, compiles, and the Output window shows `Fenrir: listening on port 8123`. If the import compiles without the `System.Web.Extensions` reference and fails, add the reference step to the top of `INSTALL.md` §1 for zip installs too.

- [ ] **Step 4: Link the guide from the README**

In `README.md`, add after the "Goals" section:

```markdown
## NinjaTrader add-on

Live and sim trading need NinjaTrader 8 on Windows with the Fenrir add-on: see [`addon/INSTALL.md`](addon/INSTALL.md).
```

- [ ] **Step 5: Commit**

```bash
git add addon/CHECKLIST.md README.md
git commit -m "docs(addon): add sim checklist and link the install guide

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
