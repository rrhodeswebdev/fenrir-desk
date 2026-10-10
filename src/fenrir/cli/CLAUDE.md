# src/fenrir/cli/

The `fenrir` command-line interface (Typer). Thin: parses arguments, calls the core, prints.

- Entry points: `main.py` (`app`, root group). Each command group is its own module (`data.py`, `backtest.py`, ...) registered in `main.py` with `app.add_typer(<group>.app, name="<group>")`.
- Pure vs shell: all shell. No logic beyond argument handling and output formatting.
- Commands accept `--json` where output is read by tools or Claude.
- Not here: arming or disarming strategies. No CLI command may arm or disarm (spec §7).
- Tests: `tests/cli/`.
