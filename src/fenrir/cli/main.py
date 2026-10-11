import typer

from fenrir.cli import backtest, compare, data

app = typer.Typer(no_args_is_help=True)
app.add_typer(data.app, name="data")
app.command("backtest")(backtest.backtest)
app.command("evaluate")(backtest.evaluate)
app.command("compare")(compare.compare_command)


@app.callback()
def main() -> None:
    """Fenrir Desk: backtest and run prop-firm futures strategies."""
