# scripts/

One-off and maintenance scripts run by hand. The app never imports anything here.

- Entry points: `lse_gate.py` (spec §3 data gate items 1-4), `gate_item5.py` (item 5: a sim account's journalled fills vs its journalled LSE bars; no LSE call); results in `docs/data-gate.md`.
- Pure vs shell: pure helpers are unit-tested in `tests/scripts/`; provider calls sit in clearly marked functions at the top of each script.
- Not here: app code (`src/fenrir/`); the app's LSE fetch lives in `src/fenrir/data/lse.py`.
- Outputs go to `data/gate/` (gitignored).
