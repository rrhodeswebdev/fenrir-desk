import typer.main
from typer.testing import CliRunner

from fenrir.cli.main import app


def test_help_describes_fenrir():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Fenrir Desk" in result.output


def _names(command):
    yield command.name or ""
    for sub in getattr(command, "commands", {}).values():
        yield from _names(sub)


def test_no_cli_command_arms_or_disarms():
    """SPEC: arming is user-only, so Claude in the terminal can neither arm nor disarm, nor
    press the kill switch (it disarms) or Resolve (22.6: UI-only)."""
    words = {w for n in _names(typer.main.get_command(app)) for w in n.lower().split("-")}
    assert "backtest" in words  # the walk reaches the commands
    assert not words & {"arm", "disarm", "arming", "armed", "kill", "resolve"}
