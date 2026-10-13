# addon/

The NinjaTrader 8 add-on (C# NinjaScript, .NET Framework 4.8): HTTP + WebSocket bridge for the managed accounts' positions, orders and fills, plus the independent 16:40 ET flatten.

- Entry points: `Fenrir/FenrirAddOn.cs` (lifecycle, `FenrirConfig`, `FenrirLog`), `Fenrir/FenrirServer.cs` (routes, token check, `/stream` fan-out and 2 s heartbeat), `Fenrir/FenrirBroker.cs` (NinjaTrader `Account` API calls on one worker thread, brackets, dedupe, event JSON).
- Pure vs shell: all shell; keep it thin. No market data, no trading decisions. API shape: `docs/contracts.md` ("Plan 19/20"); the Python client (`src/fenrir/bridge/client.py`) validates every reply strictly, so match it exactly.
- Compiles only inside NinjaTrader on Windows. Write C# 5 (no `$""`, `?.`, `=>` members, `nameof`); check NinjaTrader member names against its DLLs, never guess. Never let an exception escape a NinjaTrader event handler or timer.
- Not here: the Python client and reconciliation (`src/fenrir/bridge/`); when to trade (`src/fenrir/live/`).
- Install and Windows checks: `INSTALL.md`. No automated tests: verified by hand on Sim101.
