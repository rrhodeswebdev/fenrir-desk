import pytest
from live_helpers import ARMED_AT, RULES

from fenrir.backtest.run import AccountChoice
from fenrir.live import core
from fenrir.live.arming import Armed, ArmRequest, Refusal, decide, liquidation_level
from fenrir.rules.loader import load_account

TUNING = {
    "start": "2019-01-02",
    "end": "2025-10-09",
    "attempts": 1700,
    "pass_rate": 0.31,
    "p_pass_and_payout": 0.19,
    "expected_gross_per_attempt": 187.5,
}
CHOICES = {"lucid-flex-50k": AccountChoice(eval_price=100.0, dll_on=False)}
FLEX = load_account("lucid-flex-50k", RULES)  # end_of_day, 2000, locks at 2100
DIRECT = load_account("lucid-direct-50k", RULES)  # funded only
DAILY = load_account("lucid-daily-50k", RULES)  # intraday trailing
# the live confirmation: tuning, its expected net, the hold-out flag and what arming pins
SUMMARY = TUNING | {
    "expected_net_per_attempt": 87.5,
    "holdout_gap": False,
    "strategy_hash": "h1",
    "rules_hash": "r1",
    "dll_on": False,
    "liquidation_level": 48_500.0,  # 50 000 + max(-2000, 500 - 2000)
}


def _req(**kw) -> ArmRequest:
    base = dict(
        account="SIM1",
        strategy_path="strategies/orb.py",
        rules_account="lucid-flex-50k",
        contract="MNQ",
        mode="sim",
        phase="evaluation",
        confirm=False,
        start_balance=50_000.0,
        peak_balance=50_500.0,
    )
    return ArmRequest(**(base | kw))


def _confirmed(**kw) -> ArmRequest:
    """A live confirm echoing what the summary showed."""
    seen = {"strategy_hash": "h1", "rules_hash": "r1", "dll_on": False}
    return _req(**({"account": "LIVE1", "mode": "live", "confirm": True} | seen | kw))


def _report(
    strategy_hash="h1", account="lucid-flex-50k", contract="MNQ", dll_on=False, rules_hash="r1"
):
    return {
        "strategy": "orb",
        "strategy_hash": strategy_hash,
        "rules_hash": rules_hash,
        "account": account,
        "contract": contract,
        "dll_on": dll_on,
        "periods": {"tuning": TUNING, "holdout": None},
        "holdout_gap": False,
    }


def _decide(req, armed=(), strategy_hash="h1", reports=None, is_sim=True, rules=FLEX):
    reports = [_report()] if reports is None else reports
    return decide(
        req, armed, "orb", strategy_hash, "r1", "i1", reports, is_sim, rules, CHOICES, ARMED_AT
    )


def test_no_reports_is_no_backtest():
    assert _decide(_req(), reports=[]) == Refusal("no_backtest")


def test_report_for_other_contract_does_not_count():
    assert _decide(_req(contract="NQ"), reports=[_report(contract="MNQ")]) == Refusal("no_backtest")


def test_report_for_other_account_type_does_not_count():
    assert _decide(_req(), reports=[_report(account="lucid-pro-50k")]) == Refusal("no_backtest")


def test_old_hash_only_is_hash_changed():
    assert _decide(_req(), strategy_hash="h2", reports=[_report(strategy_hash="h1")]) == Refusal(
        "hash_changed"
    )


def test_report_with_other_dll_setting_is_dll_mismatch():
    assert _decide(_req(), reports=[_report(dll_on=True)]) == Refusal("dll_mismatch")


def test_reports_for_the_strategy_but_not_the_current_rules_are_rules_changed():
    assert _decide(_req(), reports=[_report(rules_hash="r0")]) == Refusal("rules_changed")
    # the strategy check comes first: neither hash current is hash_changed
    old = [_report(strategy_hash="h0", rules_hash="r0")]
    assert _decide(_req(), reports=old) == Refusal("hash_changed")


def test_the_report_with_the_current_rules_is_used():
    other = TUNING | {"p_pass_and_payout": 0.5}
    reports = [_report(rules_hash="r0") | {"periods": {"tuning": other, "holdout": None}}]
    reports.append(_report())
    assert _decide(_req(account="LIVE1", mode="live"), is_sim=False, reports=reports) == Refusal(
        "needs_confirmation", SUMMARY
    )


def test_sim_with_current_backtest_arms_and_pins_its_inputs():
    """DEC-4d: dll_on, the rules and instruments.yaml hashes and the balances are pinned."""
    assert _decide(_req()) == Armed(
        "SIM1",
        "strategies/orb.py",
        "h1",
        "lucid-flex-50k",
        "MNQ",
        "sim",
        "evaluation",
        ARMED_AT,
        dll_on=False,
        rules_hash="r1",
        instruments_hash="i1",
        start_balance=50_000.0,
        peak_balance=50_500.0,
    )


def test_a_dry_run_pins_no_balances():
    result = _decide(_req(mode="dry", account="paper"), is_sim=None)
    assert (result.dll_on, result.rules_hash, result.instruments_hash) == (False, "r1", "i1")
    assert (result.start_balance, result.peak_balance) == (None, None)


def test_live_without_confirm_needs_confirmation_with_the_summary():
    assert _decide(_req(account="LIVE1", mode="live"), is_sim=False) == Refusal(
        "needs_confirmation", SUMMARY
    )


def test_live_summary_flags_a_holdout_gap():
    reports = [_report() | {"holdout_gap": True}]
    result = _decide(_req(account="LIVE1", mode="live"), is_sim=False, reports=reports)
    assert result.summary["holdout_gap"] is True


def test_live_with_confirm_arms():
    result = _decide(_confirmed(), is_sim=False)
    assert isinstance(result, Armed) and result.mode == "live"


@pytest.mark.parametrize(
    "seen",
    [
        {"strategy_hash": "h0"},
        {"rules_hash": "r0"},
        {"dll_on": True},
        {"strategy_hash": None, "rules_hash": None, "dll_on": None},
    ],
    ids=str,
)
def test_a_confirm_of_something_else_than_now_is_not_a_confirmation(seen):
    """The terminal can swap the file and write a matching report between summary and confirm:
    only a confirm echoing the current hashes and dll_on arms; else the current summary again."""
    assert _decide(_confirmed(**seen), is_sim=False) == Refusal("needs_confirmation", SUMMARY)


@pytest.mark.parametrize("missing", ["start_balance", "peak_balance"])
def test_sim_and_live_without_balances_are_an_error(missing):
    with pytest.raises(ValueError, match="balance"):
        _decide(_req(**{missing: None}))


@pytest.mark.parametrize(
    ("rules", "phase", "start", "peak", "level"),
    [
        (FLEX, "evaluation", 50_000.0, 50_000.0, 48_000.0),  # floor = start - amount
        (FLEX, "evaluation", 50_000.0, 50_500.0, 48_500.0),  # trails the peak
        (FLEX, "evaluation", 50_000.0, 53_000.0, 50_100.0),  # locked at start + 2100 - 2000
        (DAILY, "funded", 50_000.0, 50_800.0, 48_800.0),  # intraday trailing
    ],
)
def test_liquidation_level_is_where_the_runner_starts_the_floor(rules, phase, start, peak, level):
    got = liquidation_level(rules, phase, start, peak)
    runner = core.start_account(rules, phase, ARMED_AT.date(), core.Start(start, peak, peak))
    assert got == level == start + runner.floor


@pytest.mark.parametrize(("mode", "is_sim"), [("sim", False), ("live", True)])
def test_mode_must_match_account(mode, is_sim):
    assert _decide(_req(mode=mode, confirm=True), is_sim=is_sim) == Refusal("mode_mismatch")


@pytest.mark.parametrize("mode", ["sim", "live"])
def test_unknown_nt_account_is_refused(mode):
    assert _decide(_req(mode=mode, confirm=True), is_sim=None) == Refusal("unknown_account")


@pytest.mark.parametrize(("mode", "phase"), [("Live", "evaluation"), ("live", "Funded")])
def test_a_mode_or_phase_outside_the_contract_is_an_error(mode, phase):
    with pytest.raises(ValueError, match="bad mode"):
        _decide(_req(mode=mode, phase=phase), is_sim=False)


def test_the_report_with_the_current_dll_setting_is_used():
    other = TUNING | {"p_pass_and_payout": 0.5}
    reports = [_report(dll_on=True) | {"periods": {"tuning": other, "holdout": None}}, _report()]
    result = _decide(_req(account="LIVE1", mode="live"), is_sim=False, reports=reports)
    assert result == Refusal("needs_confirmation", SUMMARY)


def test_a_report_of_another_strategy_with_the_same_hash_does_not_count():
    assert _decide(_req(), reports=[_report() | {"strategy": "fade"}]) == Refusal("no_backtest")


def test_dry_run_does_not_need_nt_account():
    assert isinstance(_decide(_req(mode="dry", account="paper"), is_sim=None), Armed)


def test_evaluation_phase_on_account_without_evaluation_is_refused():
    assert _decide(_req(phase="evaluation"), rules=DIRECT) == Refusal("no_evaluation_phase")
    assert isinstance(_decide(_req(phase="funded"), rules=DIRECT), Armed)


def test_account_with_an_armed_strategy_is_already_armed():
    existing = _decide(_req())
    assert _decide(_req(contract="NQ"), armed=[existing]) == Refusal("already_armed")
