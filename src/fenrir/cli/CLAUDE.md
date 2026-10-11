# src/fenrir/cli/

The `fenrir` command-line interface (Typer). Thin: parses arguments, calls the core, prints.

- Entry points: `main.py` (`app`, root group). Each command group is its own module (`data.py`, ...) registered in `main.py` with `app.add_typer(<group>.app, name="<group>")`; `backtest.py` provides top-level `backtest` and `evaluate` commands, registered with `app.command`; `compare.py` provides top-level `compare`, registered the same way; `serve.py` provides top-level `serve` (loads or creates `<private_dir>/session.key`, binds `127.0.0.1` and `::1`, then prints the launch link and runs uvicorn on those sockets).
- Pure vs shell: all shell. No logic beyond argument handling and output formatting.
- Commands accept `--json` where output is read by tools or Claude.
- Not here: arming or disarming strategies. No CLI command may arm or disarm (spec §7).
- Tests: `tests/cli/`.
