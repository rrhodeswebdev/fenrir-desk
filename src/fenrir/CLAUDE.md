# src/fenrir/

The `fenrir` Python package: server, CLI and every pure core the app runs on.

- Entry points: `cli/main.py` (`fenrir` command), `settings.py` (`.env` → `Settings`).
- Pure vs shell: each subpackage keeps pure logic in its own modules and I/O in thin shell functions; see each subpackage's `CLAUDE.md`.
- Not here: strategy files (`strategies/`), rules YAML (`rules/`), web UI (`web/`), NinjaTrader add-on (`addon/`).
- Tests: `tests/`, mirroring this tree.
- Cross-plan contracts (names, signatures, JSON): `docs/contracts.md`.
