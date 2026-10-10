# rules/

Prop-firm rule files: one YAML per account type and size at `rules/<firm>/<type>-<size>.yaml`, account id `<firm>-<type>-<size>` (e.g. `lucid-flex-50k`).

- Entry points: `lucid/*.yaml`. Schema and loader: `src/fenrir/rules/schema.py`, `src/fenrir/rules/loader.py`.
- Values come from the firm's own site only, never third-party summaries. Set `verified_on` and `source_url` when you check them; `verified_on: null` means not yet verified.
- Quote times (`flat_by: "16:45"`): unquoted YAML reads 16:45 as a number.
- Unknown keys are rejected. Lucid Direct has `evaluation: null`. `locks_at` is the profit above start where the trailing floor stops moving (`null` = never).
- Not here: rule logic (`src/fenrir/rules/`), contract specs (`instruments.yaml`).
- Tests: `tests/rules/test_rules_files.py` loads every file.
