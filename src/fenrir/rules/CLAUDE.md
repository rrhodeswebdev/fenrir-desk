# src/fenrir/rules/

Prop-firm account rules: the YAML schema and loader, account state, one pure function per rule kind, pass/payout gates and the pre-trade check.

- Entry points: `account.py` (`initial_state`, `build_rules`, `apply`, `passed`, `payout`, `view`, `with_dll`), `pretrade.py` (`check`), `state.py` (`AccountState`, events, `Breach`, `AccountView`), `loader.py` (`load_account`, `parse_account`, `list_accounts`, `rules_path`, `dump_account`), `writer.py` (`write_account`: `ruamel.yaml` round-trip that writes only changed values, keeps comments, quotes and `{ a: 1 }` flow maps, validates, then replaces atomically; pure `changes`), `schema.py` (`AccountRules`).
- One rule kind per module: `drawdown.py`, `daily_loss.py`, `flat_by.py`, `consistency.py`, `microscalp.py`, `contracts.py`; `ledger.book` does the accounting and runs first.
- Pure vs shell: everything is pure except `loader.py` (reads files) and `write_account` (writes them).
- Lucid mechanics this code assumes are listed in `docs/lucid-mechanics.md`. Change one module per wrong assumption.
- Money limits compare to the cent through `state.at_or_below`; float sums of commissions drift, so never compare balances with bare `<=`.
- Funded contract limit follows the `funded.scaling` tiers, chosen from the balance at the last session end (`contracts.max_minis`).
- `payout` models the first payout only: closed days at or above `min_day_profit` count toward `min_days`, consistency must hold, the request is capped at `max_amount` and must be at least `min_profit`, then `split_pct` applies.
- A `flat_by` breach means an engine failed to flatten before the cutoff (an engine-bug signal; Lucid itself auto-closes at 16:45 without failing the account).
- Not here: the YAML files (`rules/lucid/`), fills and the attempt fold (`src/fenrir/backtest/`), the live safety margin (`src/fenrir/live/`).
- Tests: `tests/rules/` (`conftest.py` has `base`, `make_state`, `et`).
