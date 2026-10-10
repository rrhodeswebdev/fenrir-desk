import typer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Fenrir Desk: backtest and run prop-firm futures strategies."""
