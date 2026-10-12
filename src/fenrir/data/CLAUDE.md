# src/fenrir/data/

Market data: pulling LSE history into Parquet, loading front-month bars, and (plan 04) building live 1-minute bars.

- Entry points: `store.py` (`SERIES` = ES, NQ, shared by `fenrir data` and `GET /api/data`; `load_bars`, `data_version`, `coverage`, `write_bars` for fixtures), `pull.py` (`pull_windows`, `todo`, `pull`), `lse.py` (LSE fetch), `live.py` (`Tick`, `empty_builder`, `feed`, `close_due`: ticks → closed 1-minute bars).
- Pure vs shell: `pull_windows`, `todo`, `find_gaps`, `with_contract` and the `live.py` builder are pure; `store.py` file I/O and `pull()` are shell.
- Writes (DEC-1, #135): `data/` is terminal-writable in the image and the server's pull writes as root, so every store write goes through `_atomic_write(data_dir, path, write)`: it opens `data_dir` and each directory below it with `O_DIRECTORY|O_NOFOLLOW` (a dir-fd chain, creating missing ones with `mkdir(dir_fd=)`), creates a random-named temp file `O_EXCL|O_NOFOLLOW` (mode 0644) in the last one, then `os.replace`s it over `path` within that fd. A symlink on the way, even one swapped in mid-write, raises `ValueError` and writes nothing; a symlink at `path` is replaced, never followed. Without `dir_fd` (native Windows) `_atomic_write_checked` checks then writes and also refuses a symlinked `path`. `read_manifest` (read by the root pull) refuses a symlink, a non-regular file or one over 16 MB (`MAX_MANIFEST`) with `ValueError`. Other reads (`load_bars`, `coverage`, `data_version`) are unchanged.
- `lse.py` is the ONLY module that knows about London Strategic Edge. Swapping providers = rewriting it alone.
- Prices are unadjusted front-month; front month comes from `fenrir.core.instruments.front_month`. Roll-window days (`fenrir.core.instruments.in_roll_window`) are never loaded or counted as gaps.
- `fenrir data summary` may list Good Fridays (e.g. 2024-03-29, 2025-04-18) as gaps: the CME calendar counts them as shortened sessions but LSE has no bars; NQ 2020-02-28 is genuinely missing (docs/data-gate.md).
- Not here: the files themselves (`data/bars/`), CLI (`cli/data.py`).
- Tests: `tests/data/`.
