# src/fenrir/bridge/

The Python side of the NinjaTrader add-on: instrument names, the add-on HTTP/WebSocket client and reconciliation. API shape in `docs/contracts.md` ("Plan 19/20").

- Entry points: `names.py` (`nt_instrument`: `("MNQ", "NQZ26")` → `"MNQ 12-26"`), `client.py` (`AddonClient`: REST calls with deduped retries, `stream()` with reconnects), `fake_addon.py` (`FakeAddon`, `running`: in-process fake add-on for tests).
- Pure vs shell: `names.py` is pure; `client.py` keeps pure parsing (`parse`, the response TypedDicts, `redact`) beside its async I/O shell; `fake_addon.py` is mutable test support, never used at runtime.
- Security: the NinjaTrader token never reaches a log or an exception; add-on JSON is untrusted and validated strictly.
- Not here: the add-on itself (`addon/`), the live runner and arming (`src/fenrir/live/`), contract codes and rollover (`src/fenrir/core/instruments.py`).
- Tests: `tests/bridge/`.
