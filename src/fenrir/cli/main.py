import typer

from fenrir.cli import data

app = typer.Typer(no_args_is_help=True)
app.add_typer(data.app, name="data")


@app.callback()
def main() -> None:
    """Fenrir Desk: backtest and run prop-firm futures strategies."""
