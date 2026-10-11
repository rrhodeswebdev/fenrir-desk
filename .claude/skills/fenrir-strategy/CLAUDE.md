# .claude/skills/fenrir-strategy/

The project skill Claude Code loads when working on strategies. `SKILL.md` is the content: read rules → inspect data → write strategy → `uv run fenrir backtest` → read report → iterate on the tuning period; `evaluate` only at the end.

- Entry point: `SKILL.md` (frontmatter `name`, `description`).
- Keep it in step with the CLI and the strategy contract: `tests/test_fenrir_skill.py` fails when a named `uv run fenrir` command or `--flag` disappears, or the example strategy stops loading.
- The "never read `.env`, the session token or `data/fenrir.db`" rule stands in for terminal isolation until DEC-1 (#10) is decided; revisit it then.
- Not here: strategy files (`strategies/`), rules (`rules/lucid/`), the CLI (`src/fenrir/cli/`).
