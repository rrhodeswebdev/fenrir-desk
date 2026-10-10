import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated

import typer

from fenrir.core.instruments import load_instruments
from fenrir.core.sessions import trading_day
from fenrir.data.lse import fetch_contract
from fenrir.data.pull import Window
from fenrir.data.pull import pull as run_pull
from fenrir.data.store import coverage, data_version
from fenrir.settings import load_settings

DEFAULT_SINCE = "2016-06-01"  # earliest usable day (docs/data-gate.md)
SERIES = ("ES", "NQ")

app = typer.Typer(no_args_is_help=True, help="Market data: pull LSE history and show coverage.")


@app.command()
def pull(
    series: Annotated[list[str] | None, typer.Option("--series", help="ES and/or NQ")] = None,
    since: Annotated[str, typer.Option(help="first trading day, YYYY-MM-DD")] = DEFAULT_SINCE,
) -> None:
    """Download 1-minute bars from LSE into data/bars/."""
    # Validate and normalize series
    normalized_series = [s.upper() for s in (series or list(SERIES))]
    for s in normalized_series:
        if s not in SERIES:
            typer.echo(f"unknown series {s}; choose from {', '.join(SERIES)}", err=True)
            raise typer.Exit(1)

    # Validate since date format
    try:
        since_date = date.fromisoformat(since)
    except ValueError:
        typer.echo(f"--since must be YYYY-MM-DD, got {since}", err=True)
        raise typer.Exit(1) from None

    settings = load_settings()
    if not settings.lse_api_key:
        typer.echo("FENRIR_LSE_API_KEY is not set. Add it to .env.", err=True)
        raise typer.Exit(1)

    def fetch(w: Window):
        typer.echo(f"{w.contract} {w.first}..{w.last}")
        return fetch_contract(settings, w)

    windows = run_pull(
        settings.data_dir,
        load_instruments(),
        tuple(normalized_series),
        since_date,
        trading_day(datetime.now(UTC)),
        fetch,
        lambda: datetime.now(UTC).isoformat(),
    )
    version = data_version(settings.data_dir)[:12]
    typer.echo(f"pulled {len(windows)} contract windows; data version {version}")


@app.command()
def summary(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show downloaded coverage and gaps per series."""
    settings = load_settings()
    covs = [coverage(settings.data_dir, s) for s in SERIES]
    if as_json:
        out = {"data_version": data_version(settings.data_dir), "series": [asdict(c) for c in covs]}
        typer.echo(json.dumps(out, default=str))
        return
    if all(c.days == 0 for c in covs):
        typer.echo("No bars yet. Run: fenrir data pull")
        return
    for c in covs:
        typer.echo(
            f"{c.series}  {c.first_day} .. {c.last_day}  {c.days} trading days  {len(c.gaps)} gaps"
        )
        for a, b in c.gaps:
            typer.echo(f"    missing {a} .. {b}")
