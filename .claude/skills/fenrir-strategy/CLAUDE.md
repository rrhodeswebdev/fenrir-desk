# .claude/skills/fenrir-strategy/

The project skill Claude Code loads when working on strategies. `SKILL.md` is the content: read rules → inspect data → write strategy → `uv run fenrir backtest` → read report → iterate on the tuning period; `evaluate` only at the end.

- Entry point: `SKILL.md` (frontmatter `name`, `description`).
- Keep it in step with the CLI and the strategy contract: `tests/test_fenrir_skill.py` fails when a named `uv run fenrir` command or `--flag` disappears, or the example strategy stops loading.
- DEC-1 (#10) is decided: in Docker the terminal runs as `claude` and cannot read secrets, the session key or `fenrir.db`; natively it can, so the skill's "never read" rule stays as the guard there.
- Not here: strategy files (`strategies/`), rules (`rules/lucid/`), the CLI (`src/fenrir/cli/`).
