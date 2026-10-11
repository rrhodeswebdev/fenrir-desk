# data/

Local runtime data. Gitignored except this file; may be large and licensed. Never commit its contents.

- `bars/{ES,NQ}/1m/<contract>.parquet`: 1-minute bars per contract (`ts` = bar open, UTC). Written by `fenrir data pull`.
- `bars/manifest.json`: what was pulled; its sha256 is the data version used in result caching.
- `accounts.yaml`: per account id, the user's `eval_price` and `dll_on`; read by `fenrir.backtest.run.load_account_choices`; an absent file, account or key means price 0 and DLL off.
- `results/`: backtest report JSON (plan 10). `jobs/`: backtest job state (plan 14). `fenrir.db`: SQLite journal and armed strategies (plan 21). `gate/`: data-gate script output.
- Read through `fenrir.data.store` (`load_bars`, `coverage`), not by globbing files directly. Each contract file holds LSE's continuous series for that contract's calendar window; roll-window days in it can be the expiring contract's prices and are never loaded.
- Location set by `FENRIR_DATA_DIR` (default `data`).
