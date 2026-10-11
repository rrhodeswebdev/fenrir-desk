"""Rank reports for comparison by tuning results only; hold-out is shown, never sorted on. Pure."""

from collections.abc import Mapping, Sequence
from typing import Literal

from fenrir.backtest.report import Report
from fenrir.backtest.run import AccountChoice, net_per_attempt

View = Literal["account", "strategy"]


def summarize(report: Report) -> dict:
    """The report without `sample_equity` in any period."""
    periods = {
        name: None if p is None else {k: v for k, v in p.items() if k != "sample_equity"}
        for name, p in report["periods"].items()
    }
    return {**report, "periods": periods}


def with_net(report: Report, choices: Mapping[str, AccountChoice]) -> dict:
    """Every period also carries `expected_net_per_attempt` (gross − that account's eval_price)."""
    periods = {
        name: None
        if p is None
        else {**p, "expected_net_per_attempt": net_per_attempt(p, report["account"], choices)}
        for name, p in report["periods"].items()
    }
    return {**report, "periods": periods}


def latest_only(reports: Sequence[Report]) -> tuple[Report, ...]:
    """Input newest first; keeps the newest per (strategy, account, contract), in input order."""
    seen: set[tuple[str, str, str]] = set()
    out = []
    for r in reports:
        triple = (r["strategy"], r["account"], r["contract"])
        if triple not in seen:
            seen.add(triple)
            out.append(r)
    return tuple(out)


def compare(
    reports: Sequence[Report], view: View, choices: Mapping[str, AccountChoice]
) -> list[dict]:
    """`[{group, rows}]`: groups by name; rows summarized, with net, best tuning result first."""

    def rank(r: Report) -> tuple:
        t = r["periods"]["tuning"]
        net = net_per_attempt(t, r["account"], choices)
        return (-t["p_pass_and_payout"], -net, r["strategy"], r["account"], r["contract"])

    return [
        {
            "group": g,
            "rows": [
                with_net(summarize(r), choices)
                for r in sorted((r for r in reports if r[view] == g), key=rank)
            ],
        }
        for g in sorted({r[view] for r in reports})
    ]
