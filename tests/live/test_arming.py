import pytest
from live_helpers import ARMED_AT

from fenrir.backtest.run import AccountChoice
from fenrir.live.arming import Armed, ArmRequest, Refusal, decide

TUNING = {
    "start": "2019-01-02",
    "end": "2025-10-09",
    "attempts": 1700,
    "pass_rate": 0.31,
    "p_pass_and_payout": 0.19,
    "expected_gross_per_attempt": 187.5,
}
CHOICES = {"lucid-flex-50k": AccountChoice(eval_price=100.0, dll_on=False)}


def _req(**kw) -> ArmRequest:
    base = dict(
        account="SIM1",
        strategy_path="strategies/orb.py",
        rules_account="lucid-flex-50k",
        contract="MNQ",
        mode="sim",
        phase="evaluation",
        confirm=False,
    )
    return ArmRequest(**(base | kw))


def _report(strategy_hash="h1", account="lucid-flex-50k", contract="MNQ", dll_on=False):
    return {
        "strategy": "orb",
        "strategy_hash": strategy_hash,
        "account": account,
        "contract": contract,
        "dll_on": dll_on,
        "periods": {"tuning": TUNING, "holdout": None},
    }


def _decide(req, armed=(), strategy_hash="h1", reports=None, is_sim=True, has_evaluation=True):
    reports = [_report()] if reports is None else reports
    return decide(
        req, armed, "orb", strategy_hash, reports, is_sim, has_evaluation, CHOICES, ARMED_AT
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


def test_sim_with_current_backtest_arms():
    assert _decide(_req()) == Armed(
        "SIM1", "strategies/orb.py", "h1", "lucid-flex-50k", "MNQ", "sim", "evaluation", ARMED_AT
    )


def test_live_without_confirm_needs_confirmation_with_tuning_summary():
    assert _decide(_req(account="LIVE1", mode="live"), is_sim=False) == Refusal(
        "needs_confirmation", TUNING | {"expected_net_per_attempt": 87.5}
    )


def test_live_with_confirm_arms():
    result = _decide(_req(account="LIVE1", mode="live", confirm=True), is_sim=False)
    assert isinstance(result, Armed) and result.mode == "live"


@pytest.mark.parametrize(("mode", "is_sim"), [("sim", False), ("live", True)])
def test_mode_must_match_account(mode, is_sim):
    assert _decide(_req(mode=mode, confirm=True), is_sim=is_sim) == Refusal("mode_mismatch")


def test_unknown_nt_account_is_refused_for_sim():
    assert _decide(_req(), is_sim=None) == Refusal("unknown_account")


def test_dry_run_does_not_need_nt_account():
    assert isinstance(_decide(_req(mode="dry", account="paper"), is_sim=None), Armed)


def test_evaluation_phase_on_account_without_evaluation_is_refused():
    assert _decide(_req(phase="evaluation"), has_evaluation=False) == Refusal("no_evaluation_phase")
    assert isinstance(_decide(_req(phase="funded"), has_evaluation=False), Armed)


def test_account_with_an_armed_strategy_is_already_armed():
    existing = _decide(_req())
    assert _decide(_req(contract="NQ"), armed=[existing]) == Refusal("already_armed")
