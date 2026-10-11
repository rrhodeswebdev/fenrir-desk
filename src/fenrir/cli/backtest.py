"""`fenrir backtest` (tuning only) and `fenrir evaluate` (tuning + hold-out). Thin shell."""

import json
from collections.abc import Mapping
from typing import Annotated

import typer

from fenrir.backtest.cache import cached_run
from fenrir.backtest.grid import GridItem, expand_grid
from fenrir.backtest.report import PeriodResult, Report
from fenrir.backtest.run import AccountChoice, load_account_choices, net_per_attempt
from fenrir.settings import load_settings

Strategies = Annotated[list[str], typer.Argument(help="Strategy file globs, relative to the cwd")]
Accounts = Annotated[
    list[str], typer.Option("--account", help="Rules account id glob, e.g. 'lucid-*'")
]
Contracts = Annotated[
    list[str] | None,
    typer.Option("--contract", help="ES, MES, NQ or MNQ; default both of the strategy's market"),
]
AsJson = Annotated[bool, typer.Option("--json", help="Print report JSON")]


def _period(p: PeriodResult, account: str, choices: Mapping[str, AccountChoice]) -> str:
    return (
        f"pass {p['pass_rate']:.1%} · pass+payout {p['p_pass_and_payout']:.1%} · "
        f"net ${net_per_attempt(p, account, choices):,.2f}/attempt"
    )


def format_summary(report: Report, choices: Mapping[str, AccountChoice]) -> str:
    account = report["account"]
    tuning = _period(report["periods"]["tuning"], account, choices)
    line = f"{report['strategy']}  {account}  {report['contract']}  tuning: {tuning}"
    holdout = report["periods"]["holdout"]
    if holdout is not None:
        line += f"  hold-out: {_period(holdout, account, choices)}"
    if report["holdout_gap"]:
        line += "  HOLD-OUT GAP"
    return line


def _progress(item: GridItem):
    label = f"{item.strategy_path.name} {item.account_id} {item.contract}"

    # stderr, so --json output stays clean; real runs take long enough to look hung
    def show(done: int, total: int) -> None:
        end = "\n" if done == total else ""
        typer.echo(f"\r{label}: {done}/{total} attempts{end}", err=True, nl=False)

    return show


def _run(
    strategies: list[str],
    accounts: list[str],
    contracts: list[str] | None,
    as_json: bool,
    include_holdout: bool,
) -> None:
    settings = load_settings()
    try:
        choices = load_account_choices(settings.data_dir)
        reports = [
            cached_run(item, settings, include_holdout=include_holdout, on_progress=_progress(item))
            for item in expand_grid(strategies, accounts, contracts, settings)
        ]
    except (ValueError, OSError) as e:
        typer.echo(f"\nerror: {e}", err=True)  # \n ends a partial progress line
        raise typer.Exit(1) from e
    typer.echo(
        json.dumps(reports, indent=2)
        if as_json
        else "\n".join(format_summary(r, choices) for r in reports)
    )


def backtest(
    strategies: Strategies,
    account: Accounts,
    contract: Contracts = None,
    as_json: AsJson = False,
) -> None:
    """Backtest on the tuning period only (never sees the hold-out)."""
    _run(strategies, account, contract, as_json, include_holdout=False)


def evaluate(
    strategies: Strategies,
    account: Accounts,
    contract: Contracts = None,
    as_json: AsJson = False,
) -> None:
    """Backtest on the tuning and hold-out periods, side by side."""
    _run(strategies, account, contract, as_json, include_holdout=True)
