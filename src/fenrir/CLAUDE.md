# src/fenrir/

The `fenrir` Python package: server, CLI and every pure core the app runs on.

- Entry points: `cli/main.py` (`fenrir` command), `settings.py` (`$FENRIR_ENV_FILE` or `.env`, never the repo `.env` in the image → `Settings`), `files.py` (`read_regular(path, limit)`: how the root server reads a terminal-writable file (results, jobs, accounts, Parquet bars, rules, `instruments.yaml`, strategy hashes): no symlink, FIFO, device, directory or oversized file, fd closed exactly once; `ValueError` naming the file, never quoting contents; missing/denied keep `FileNotFoundError`/`PermissionError`); `refuse_symlink(path)`: `ValueError` `<path> is a symlink; not reading it`, for `strategies_dir` before root globs or hashes it, and for `rules_dir` and its firm dirs in `list_accounts`.
- Pure vs shell: each subpackage keeps pure logic in its own modules and I/O in thin shell functions; see each subpackage's `CLAUDE.md`.
- Not here: strategy files (`strategies/`), rules YAML (`rules/`), web UI (`web/`), NinjaTrader add-on (`addon/`).
- Tests: `tests/`, mirroring this tree.
- Cross-plan contracts (names, signatures, JSON): `docs/contracts.md`.
