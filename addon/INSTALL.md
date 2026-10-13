# Installing the Fenrir NinjaTrader add-on

Needs NinjaTrader 8 on Windows: the host itself, or a Windows VM when Fenrir runs on a Mac.

## 1. Install from source

Copy the three files in `addon\Fenrir\` into NinjaTrader's add-on folder (PowerShell, from the repo root):

```powershell
Copy-Item addon\Fenrir\*.cs "$([Environment]::GetFolderPath('MyDocuments'))\NinjaTrader 8\bin\Custom\AddOns\" -Force
```

In NinjaTrader open **New → NinjaScript Editor** and press **F5** to compile. The add-on uses `System.Web.Extensions`, which NinjaTrader references by default; if the compile reports `JavaScriptSerializer` missing, right-click in the editor → **References…** → add `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll`.

## 2. Config and token

Generate one token on the machine running Fenrir (hex, so it is safe in a URL):

```bash
openssl rand -hex 32
# or: python -c "import secrets; print(secrets.token_hex(32))"
```

Copy `addon\fenrir-addon.example.json` to `Documents\NinjaTrader 8\fenrir-addon.json` and edit it:

```json
{
  "port": 8123,
  "token": "<the token>",
  "managed_accounts": ["Sim101"],
  "flatten_at": "16:40",
  "flatten_on_disconnect_seconds": null
}
```

- `token`: at least 16 characters, the same value as Fenrir's NinjaTrader token (Settings page, or `FENRIR_NT_TOKEN` natively). With a shorter token the add-on logs why and serves nothing.
- `managed_accounts`: the NinjaTrader account names Fenrir may see and trade. Every other account is invisible to Fenrir and refused. An empty list is logged as an alert at start.
- `flatten_at`: New York time (`"HH:MM"`, whatever the PC's time zone) at which the add-on, on its own, closes every position and cancels every working order on the managed accounts, then again every 15 s for 20 minutes. Keep `16:40`; change it only to test the flatten. A bad value falls back to 16:40 with an alert. The flatten runs even when the token is too short or the port cannot be opened.
- `flatten_on_disconnect_seconds`: `null` (default) keeps the broker-side stop and target when Fenrir's link drops. A number of seconds flattens every managed account holding a position once no Fenrir stream has been connected for that long, counted from NinjaTrader's start too. It runs with the add-on's HTTP server, so not while the token is too short or the port cannot be opened (the 16:40 flatten still does).

Beside it the add-on keeps `fenrir-addon.seen.json`, the client order ids of the last 7 days, so a request Fenrir retries after a NinjaTrader restart is still answered `duplicate` instead of placed twice. Leave it alone.

Don't recompile (F5) or restart NinjaTrader while Fenrir has an entry filling: the add-on keeps its brackets in memory, so a recompile in that second leaves the rest of that fill without its stop until Fenrir reconciles.

Restart NinjaTrader. **New → NinjaScript Output** shows `Fenrir: listening on port 8123`.

## 3. Allow the port (PowerShell as Administrator)

```powershell
netsh http add urlacl url=http://+:8123/ user=Everyone
netsh advfirewall firewall add rule name="Fenrir add-on 8123" dir=in action=allow protocol=TCP localport=8123 remoteip=localsubnet
```

Without the `urlacl` line the add-on cannot listen and logs `failed to start: ... Access is denied`.

- **Fenrir in Docker on the same Windows machine:** NinjaTrader address `http://host.docker.internal:8123`. If requests time out, the WSL/Docker network is not in `localsubnet`: run `ipconfig`, find the `vEthernet (WSL)` adapter's subnet and use it as `remoteip=` (for example `remoteip=172.16.0.0/12`).
- **Fenrir on a Mac, NinjaTrader in a VM:** run `ipconfig` in the VM and use `http://<its IPv4 address>:8123`. Use bridged or shared networking so the Mac and the VM share a subnet.

## 4. Check

From the machine running Fenrir, with `T` set to the token and `H` to the NinjaTrader address:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$H/accounts"                            # 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Fenrir-Token: wrong" "$H/accounts" # 401
curl -s -H "X-Fenrir-Token: $T" "$H/accounts"                                     # [{"id":"Sim101",...,"is_sim":true,...}]
```

The event stream (heartbeat every 2 s, then account, order, position and fill events):

```bash
uv run --with websockets python -m websockets "${H/http/ws}/stream?token=$T"
```
