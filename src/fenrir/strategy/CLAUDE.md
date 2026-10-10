# src/fenrir/strategy/

The strategy contract: what a strategy file must export, the values its `step` receives, and the loader that imports a strategy file.

- Entry points: `types.py` (`Position`, `Ctx`, `FLAT`), `loader.py` (`Strategy`, `load_strategy`, `file_hash`).
- Pure vs shell: `types.py` is pure data; `loader.py` reads and executes files (shell).
- Not here: strategy files themselves (`strategies/`), running them over bars (`src/fenrir/backtest/`), running them live (`src/fenrir/live/`).
- Tests: `tests/strategy/`.
