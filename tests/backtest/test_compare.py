from fenrir.backtest.compare import compare, latest_only, summarize, with_net
from fenrir.backtest.run import AccountChoice


def period(p: float, gross: float) -> dict:
    return {
        "attempts": 10,
        "pass_rate": 0.5,
        "p_pass_and_payout": p,
        "expected_gross_per_attempt": gross,
        "sample_equity": [{"start": "2020-01-02", "outcome": "payout", "points": []}],
    }


def report(
    strategy,
    account,
    contract="NQ",
    tuning=(0.2, 50.0),
    holdout=(0.2, 50.0),
    created="2026-10-10T00:00:00Z",
    dll_on=False,
    data_version="d",
):
    return {
        "key": f"{strategy}-{account}-{contract}-{created}",
        "created_at": created,
        "strategy": strategy,
        "strategy_hash": "h",
        "account": account,
        "rules_hash": "r",
        "contract": contract,
        "data_version": data_version,
        "holdout_months": 12,
        "dll_on": dll_on,
        "periods": {
            "tuning": period(*tuning),
            "holdout": None if holdout is None else period(*holdout),
        },
        "holdout_gap": False,
    }


def strategies(group: dict) -> list[str]:
    return [r["strategy"] for r in group["rows"]]


def test_summarize_drops_sample_equity_everywhere():
    s = summarize(report("orb", "a"))
    assert "sample_equity" not in s["periods"]["tuning"]
    assert "sample_equity" not in s["periods"]["holdout"]
    assert summarize(report("orb", "a", holdout=None))["periods"]["holdout"] is None


def test_with_net_subtracts_that_accounts_eval_price():
    r = with_net(
        report("orb", "a", tuning=(0.2, 50.0), holdout=(0.1, -20.0)),
        {"a": AccountChoice(eval_price=30.0)},
    )
    assert r["periods"]["tuning"]["expected_net_per_attempt"] == 20.0
    assert r["periods"]["holdout"]["expected_net_per_attempt"] == -50.0
    assert with_net(report("orb", "b", holdout=None), {})["periods"]["holdout"] is None


def test_latest_only_keeps_newest_per_triple():
    newer = report("orb", "a", created="2026-10-10T02:00:00Z")
    older = report("orb", "a", created="2026-10-10T01:00:00Z")
    other = report("orb", "a", contract="MNQ")
    assert latest_only([newer, older, other], {}) == (newer, other)


def test_latest_only_keeps_only_the_accounts_current_dll_setting():
    dll = report("orb", "a", created="2026-10-10T02:00:00Z", dll_on=True)
    no_dll = report("orb", "a", created="2026-10-10T01:00:00Z")
    only_dll = report("orb", "b", dll_on=True)
    assert latest_only([dll, no_dll, only_dll], {}) == (no_dll,)  # b has no current report
    on = {"a": AccountChoice(dll_on=True)}
    assert latest_only([dll, no_dll, only_dll], on) == (dll,)


def test_latest_only_keeps_holdout_over_a_newer_tuning_only_run_of_the_same_inputs():
    tuning_only = report("orb", "a", holdout=None, created="2026-10-10T03:00:00Z")
    evaluated = report("orb", "a", created="2026-10-10T02:00:00Z")
    older = report("orb", "a", created="2026-10-10T01:00:00Z")
    assert latest_only([tuning_only, evaluated, older], {}) == (evaluated,)
    stale = report("orb", "a", created="2026-10-10T02:00:00Z", data_version="old")
    assert latest_only([tuning_only, stale], {}) == (tuning_only,)  # other inputs: newest wins


def test_by_account_ranks_by_tuning_then_net():
    rows = [
        report("low", "a", tuning=(0.1, 10.0)),
        report("high", "a", tuning=(0.3, 10.0)),
        report("tie_rich", "a", tuning=(0.1, 90.0)),
        report("x", "b"),
    ]
    out = compare(rows, "account", {})
    assert [g["group"] for g in out] == ["a", "b"]
    assert strategies(out[0]) == ["high", "tie_rich", "low"]


def test_ties_break_by_net_after_each_accounts_eval_price():
    # same gross, same p: the cheaper evaluation nets more per attempt
    rows = [
        report("orb", "a-pricey", tuning=(0.2, 100.0)),
        report("orb", "z-cheap", tuning=(0.2, 100.0)),
    ]
    choices = {
        "a-pricey": AccountChoice(eval_price=90.0),
        "z-cheap": AccountChoice(eval_price=10.0),
    }
    (group,) = compare(rows, "strategy", choices)
    assert [r["account"] for r in group["rows"]] == ["z-cheap", "a-pricey"]
    assert [r["periods"]["tuning"]["expected_net_per_attempt"] for r in group["rows"]] == [
        90.0,
        10.0,
    ]
    assert "sample_equity" not in group["rows"][0]["periods"]["tuning"]


def test_by_strategy_groups_by_strategy():
    rows = [
        report("orb", "lucid-pro-50k", tuning=(0.1, 0.0)),
        report("orb", "lucid-flex-50k", tuning=(0.4, 0.0)),
        report("vwap", "lucid-flex-50k"),
    ]
    out = compare(rows, "strategy", {})
    assert [g["group"] for g in out] == ["orb", "vwap"]
    assert [r["account"] for r in out[0]["rows"]] == ["lucid-flex-50k", "lucid-pro-50k"]


def test_holdout_never_changes_order():
    a = report("a", "acct", tuning=(0.3, 10.0), holdout=(0.0, -500.0))
    b = report("b", "acct", tuning=(0.2, 10.0), holdout=(0.9, 900.0))
    c = report("c", "acct", tuning=(0.2, 10.0), holdout=None)
    expected = ["a", "b", "c"]
    assert strategies(compare([c, b, a], "account", {})[0]) == expected
    assert strategies(compare([b, c, a], "account", {})[0]) == expected
