# src/fenrir/data/

Market data: pulling LSE history into Parquet, loading front-month bars, and (plan 04) building live 1-minute bars.

- Entry points: `store.py` (`load_bars`, `data_version`, `coverage`, `write_bars` for fixtures), `pull.py` (`pull_windows`, `todo`, `pull`), `lse.py` (LSE fetch), `live.py` (`Tick`, `empty_builder`, `feed`, `close_due`: ticks → closed 1-minute bars).
- Pure vs shell: `pull_windows`, `todo`, `find_gaps`, `with_contract` and the `live.py` builder are pure; `store.py` file I/O and `pull()` are shell.
- `lse.py` is the ONLY module that knows about London Strategic Edge. Swapping providers = rewriting it alone.
- Prices are unadjusted front-month; front month comes from `fenrir.core.instruments.front_month`. Roll-window days (`fenrir.core.instruments.in_roll_window`) are never loaded or counted as gaps.
- `fenrir data summary` may list Good Fridays (e.g. 2024-03-29, 2025-04-18) as gaps: the CME calendar counts them as shortened sessions but LSE has no bars; NQ 2020-02-28 is genuinely missing (docs/data-gate.md).
- Not here: the files themselves (`data/bars/`), CLI (`cli/data.py`).
- Tests: `tests/data/`.
