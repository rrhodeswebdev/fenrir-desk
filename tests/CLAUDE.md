# tests/

pytest suite. Mirrors `src/fenrir/`: `tests/<pkg>/test_<module>.py` tests `src/fenrir/<pkg>/<module>.py`.

- Run: `uv run pytest`. Runs with `--import-mode=importlib`, so no `__init__.py` files are needed.
- `tests/scripts/` tests pure helpers inside `scripts/` (loaded by file path).
- `tests/fixtures/` holds small hand-built data files shared between tests.
- Pure core gets example tests with hand-computed values; property tests (Hypothesis) where a plan calls for them.
- Not here: Playwright browser tests (`web/e2e/`), add-on manual checklist (`addon/`).
