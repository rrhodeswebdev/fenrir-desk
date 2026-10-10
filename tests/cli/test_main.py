from typer.testing import CliRunner

from fenrir.cli.main import app


def test_help_describes_fenrir():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Fenrir Desk" in result.output
