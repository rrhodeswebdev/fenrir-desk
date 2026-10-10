# src/fenrir/rules/

Prop-firm account rules: the YAML schema, loading account files, and (plan 06) the pure rule functions that judge an account's state.

- Entry points: `schema.py` (`AccountRules` and its sub-models), `loader.py` (`load_account`, `list_accounts`, `rules_path`, `dump_account`, `account_id`).
- Pure vs shell: `schema.py` is pure; `loader.py` reads files (shell).
- Not here: the YAML files themselves (`rules/lucid/`), contract specs (`src/fenrir/core/instruments.py`).
- Tests: `tests/rules/` (`conftest.py` has the `base` rules-dict fixture).
