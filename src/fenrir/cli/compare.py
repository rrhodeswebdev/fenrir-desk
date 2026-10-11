"""`fenrir compare`: cached reports ranked by tuning results, by account or strategy. Shell."""

import json
from typing import Annotated

import typer

from fenrir.backtest.cache import list_reports
from fenrir.backtest.compare import View, compare, latest_only
from fenrir.backtest.run import load_account_choices
from fenrir.cli.backtest import AsJson, format_summary
from fenrir.settings import load_settings


def compare_command(
    view: Annotated[View, typer.Option("--view", help="Group rows by account or by strategy")],
    as_json: AsJson = False,
) -> None:
    """Rank the latest cached report per strategy, account and contract (tuning results only)."""
    settings = load_settings()
    try:
        choices = load_account_choices(settings.data_dir)
        groups = compare(latest_only(list_reports(settings)), view, choices)
    except (ValueError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    if as_json:
        typer.echo(json.dumps(groups, indent=2))
        return
    for g in groups:
        typer.echo(g["group"])
        for row in g["rows"]:
            typer.echo(f"  {format_summary(row, choices)}")
