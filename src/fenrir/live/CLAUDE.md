# src/fenrir/live/

Runs armed strategies on live bars: decides per bar, checks limits, journals every decision and stores which strategy is armed on each account. Rolls the trading day (lifting day halts), pauses entries on stale data, emits the orders to send (`Send`) and books the add-on's fills. The hub that performs sends lands here in later plan 21–23 tasks.

- Entry points: `core.py` (pure runner: `start`, `on_bar`, `on_tick`, `on_fill`, `send_failed`, `client_id`, `in_session`, `ctx`, `decide`, `step_failed`, `halt`, `account_status`, `safety_level`, `start_account`; `Setup`, `Start`, `RunnerState`, `Working`, `Log`, `Send`), `journal.py` (`open_db` creates `journal` and `armed` in `<private_dir>/fenrir.db`; `write`, `rows`, `day_bounds`, `JournalRow`, `KINDS`), `arming.py` (`Armed`, `list_armed`, `insert_armed`, `delete_armed`; `ArmRequest`, `Refusal`, `decide`: may this file be armed on this account, one `Refusal` per 409 reason).
- Pure vs shell: `core.py` is pure `(RunnerState, input) -> (RunnerState, effects)` and never imports strategy code: the shell runs `step(ctx(state))` in a child (DEC-1) and passes the actions to `decide`. `journal.py` and `arming.py` are shell (SQLite I/O); `day_bounds` and `decide` are pure (the caller passes `list_armed`, reports, NinjaTrader's `is_sim`, account choices and `now`).
- Pre-trade decisions must match the backtest's (`fenrir.backtest.attempt._act`); `test_accepts_exactly_what_the_backtest_accepts` pins it.
- Fills book the account like the backtest (`attempt._open`/`_close`/`_mark`); `test_live_fills_book_like_the_backtest` pins it. Dry runs never emit a `Send`.
- Never add a CLI command that arms or disarms, and never write the `armed` table outside `arming.py` (SPEC: arming is user-only).
- Not here: arm/disarm endpoints (server, plan 23); the add-on client (`src/fenrir/bridge/`); rule math (`src/fenrir/rules/`).
- Tests: `tests/live/` (shared helpers in `tests/live/live_helpers.py`, imported by name).
