"""The fenrir-strategy skill stays in step with the CLI and the strategy contract."""

import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fenrir.cli.main import app
from fenrir.strategy.loader import load_strategy

SKILL = Path(__file__).parents[1] / ".claude/skills/fenrir-strategy/SKILL.md"
FLAG = re.compile(r"--[a-z][a-z-]*")


def text() -> str:
    return SKILL.read_text()


def commands(skill: str) -> list[tuple[list[str], list[str]]]:
    """(command words, flags) for every `uv run fenrir ...` in the skill."""
    found = []
    for rest in re.findall(r"uv run fenrir ([^`\n]*)", skill):
        tokens = rest.split()
        words = []
        for token in tokens:  # command words lead; the first path, glob, quote or flag ends them
            if not re.fullmatch(r"[a-z]+", token):
                break
            words.append(token)
        found.append((words, FLAG.findall(rest)))
    return found


def drift(skill: str) -> list[str]:
    """Commands and flags the skill names that the CLI does not have."""
    named = commands(skill)
    problems = []
    if not named:
        problems.append("skill names no `uv run fenrir` commands")
    for words, flags in named:
        result = CliRunner().invoke(app, [*words, "--help"])
        if result.exit_code != 0:
            problems.append(f"`fenrir {' '.join(words)}` does not exist")
            continue
        problems += [
            f"`fenrir {' '.join(words)}` has no {flag}"
            for flag in flags
            if not re.search(rf"{flag}(?![\w-])", result.output)
        ]
    on_commands = {flag for _, flags in named for flag in flags}
    problems += [
        f"{flag} is named outside a `uv run fenrir` command"
        for flag in sorted(set(FLAG.findall(skill)) - on_commands)
    ]
    return problems


def test_frontmatter():
    front = yaml.safe_load(text().split("---")[1])
    assert front["name"] == "fenrir-strategy"
    assert "strategy" in front["description"]


def test_every_command_and_flag_in_skill_exists():
    assert drift(text()) == []


def test_drift_catches_a_missing_command_and_flag():
    skill = "`uv run fenrir backtest 'strategies/x.py' --acount lucid-flex-50k`\n"
    skill += "`uv run fenrir arm strategies/x.py`\n"
    assert drift(skill) == [
        "`fenrir backtest` has no --acount",
        "`fenrir arm` does not exist",
    ]


def test_example_strategy_loads(tmp_path):
    code = re.search(r"```python\n(# strategies/example\.py\n.*?)```", text(), re.S).group(1)
    path = tmp_path / "example.py"
    path.write_text(code)
    strategy = load_strategy(path)
    assert strategy.market in ("ES", "NQ")
    strategy.init(strategy.params)
