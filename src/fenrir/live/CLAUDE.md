# src/fenrir/live/

Runs armed strategies on live bars: journals every decision and stores which strategy is armed on each account. The runner, safety and order sending land here in later plan 21–23 tasks.

- Entry points: `journal.py` (`open_db` creates `journal` and `armed` in `data/fenrir.db`; `write`, `rows`, `day_bounds`, `JournalRow`, `KINDS`), `arming.py` (`Armed`, `list_armed`, `insert_armed`, `delete_armed`; `ArmRequest`, `Refusal`, `decide`: may this file be armed on this account, one `Refusal` per 409 reason).
- Pure vs shell: `journal.py` and `arming.py` are shell (SQLite I/O); `day_bounds` and `decide` are pure (the caller passes `list_armed`, reports, NinjaTrader's `is_sim`, account choices and `now`).
- Never add a CLI command that arms or disarms, and never write the `armed` table outside `arming.py` (SPEC: arming is user-only).
- Not here: arm/disarm endpoints (server, plan 23); the add-on client (`src/fenrir/bridge/`); rule math (`src/fenrir/rules/`).
- Tests: `tests/live/` (shared helpers in `tests/live/live_helpers.py`, imported by name).
