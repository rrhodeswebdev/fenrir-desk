# src/fenrir/bridge/

The Python side of the NinjaTrader add-on: instrument names, the add-on HTTP/WebSocket client and reconciliation. API shape in `docs/contracts.md` ("Plan 19/20").

- Entry points: `names.py` (`nt_instrument`: `("MNQ", "NQZ26")` → `"MNQ 12-26"`).
- Pure vs shell: `names.py` is pure.
- Not here: the add-on itself (`addon/`), the live runner and arming (`src/fenrir/live/`), contract codes and rollover (`src/fenrir/core/instruments.py`).
- Tests: `tests/bridge/`.
