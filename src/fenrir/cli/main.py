import typer

from fenrir.cli import backtest, compare, data, serve

app = typer.Typer(no_args_is_help=True)
app.add_typer(data.app, name="data")
app.command("backtest")(backtest.backtest)
app.command("evaluate")(backtest.evaluate)
app.command("compare")(compare.compare_command)
app.command("serve")(serve.serve)


@app.callback()
def main() -> None:
    """Fenrir Desk: backtest and run prop-firm futures strategies."""
