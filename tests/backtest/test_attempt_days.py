import pytest
from backtest_helpers import D1, D2, D3, NQ, bar, et, make_rules, scripted

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket

LONG = Bracket("long", 1, 95.0, 105.0)


def winning_day(day):
    # entry 100.75 at 09:31, target 105 at 09:32: +85
    return [
        bar("09:30", 100, 100.5, 99.5, 100, day),
        bar("09:31", 100.5, 102, 100, 101, day),
        bar("09:32", 101, 105.5, 100.5, 105, day),
    ]


def attempt(bars, script, **rules_kw):
    return run_attempt(tuple(bars), 0, len(bars), scripted(script), make_rules(**rules_kw), NQ)


def test_pass_then_first_payout():
    bars = [*winning_day(D1), *winning_day(D2), bar("09:30", 100, 100.5, 99.5, 100, D3)]
    r = attempt(bars, {"09:30": [LONG]}, target=50.0)
    assert r.outcome == "payout"
    assert r.days_to_pass == 1
    assert r.days_to_payout == 2
    # plan 06 payout: funded profit 85 x max_pct_of_profit 50% x split 90%
    assert r.first_payout == pytest.approx(85 * 0.50 * 0.90)
    assert r.equity == (
        (et(D1, "09:32"), 85.0),
        (et(D2, "09:32"), 85.0),
    )  # funded balance restarts at 0


def test_passed_but_data_ends_before_payout():
    r = attempt([*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)], {}, target=50.0)
    assert r.outcome == "no_result"  # no trades: never passed
    r = attempt(
        [*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)], {"09:30": [LONG]}, target=50.0
    )
    assert (r.outcome, r.days_to_pass) == ("passed", 1)


def test_account_without_evaluation_starts_funded():
    r = attempt(
        [*winning_day(D1), bar("09:30", 100, 100.5, 99.5, 100, D2)],
        {"09:30": [LONG]},
        evaluation=False,
    )
    assert (r.outcome, r.days_to_pass, r.days_to_payout) == ("payout", 0, 1)
    assert r.first_payout == pytest.approx(85 * 0.50 * 0.90)


def test_flat_by_forces_exit_and_blocks_entries():
    bars = [
        bar("16:37", 100, 100.5, 99.5, 100),
        bar("16:38", 100, 100.5, 99.5, 100),  # entry 100.25
        bar("16:39", 100, 101, 99.5, 100.5),  # closes 16:40: forced exit 100.5 - tick
        bar("16:40", 100, 100.5, 99.5, 100),
        bar("16:41", 100, 100.5, 99.5, 100),
    ]
    r = attempt(bars, {"16:37": [LONG], "16:39": [LONG], "16:40": [LONG]})
    assert [(f.qty, f.price, f.ts) for f in r.fills] == [
        (1, 100.25, et(D1, "16:38")),
        (-1, 100.25, et(D1, "16:39")),
    ]


def test_evening_entry_is_not_force_closed():
    bars = [
        bar("18:01", 100, 100.5, 99.5, 100),
        bar("18:02", 100, 100.5, 99.5, 100),
        bar("18:03", 100, 100.5, 99.5, 100),
    ]
    r = attempt(bars, {"18:01": [LONG]})
    assert [(f.qty, f.price) for f in r.fills] == [(1, 100.25)]


def test_data_gap_closes_position_before_day_close():
    bars = [
        bar("09:30", 100, 100.5, 99.5, 100),
        bar("09:31", 100.5, 102, 100, 101),  # entry 100.75; data then jumps to the next day
        bar("09:30", 102, 102.5, 101.5, 102, D2),  # exit at open - tick: 101.75
    ]
    r = attempt(bars, {"09:30": [LONG]})
    assert [(f.qty, f.price) for f in r.fills][:2] == [(1, 100.75), (-1, 101.75)]
    assert r.fills[1].ts == et(D2, "09:30")
    assert r.equity == ((et(D1, "09:31"), 20.0),)


def test_daily_loss_halt_lifts_next_day():
    dll_long = Bracket("long", 1, 97.0, 105.0)
    halt_day = [
        bar("09:30", 100, 100.5, 99.5, 100),
        bar("09:31", 102, 102.5, 101.5, 102),  # entry gaps to 102.25
        bar(
            "09:32", 101, 101, 97.25, 97.5
        ),  # DLL hit: flattened at 97.0, realized (97 - 102.25) * 20 = -105
    ]
    r = attempt([*halt_day, *winning_day(D2)], {"09:30": [dll_long]}, daily_loss=100.0)
    assert [(f.qty, f.price) for f in r.fills] == [
        (1, 102.25),
        (-1, 97.0),
        (1, 100.75),
        (-1, 105.0),
    ]
    assert (r.outcome, r.breach) == ("no_result", None)
    assert r.equity == (
        (et(D1, "09:32"), -105.0),
    )  # D2 trades: -105 + 85 = -20 (D2 is the last day, never closed)


def test_data_gap_day_closed_names_the_closed_day(monkeypatch):
    from fenrir.backtest import attempt as mod
    from fenrir.rules.state import DayClosed

    seen = []
    real = mod.apply

    def spy(rule_fns, account, event):
        if isinstance(event, DayClosed):
            seen.append(event.day)
        return real(rule_fns, account, event)

    monkeypatch.setattr(mod, "apply", spy)
    bars = [
        bar("09:30", 100, 100.5, 99.5, 100),
        bar("09:31", 100.5, 102, 100, 101),
        bar("09:30", 102, 102.5, 101.5, 102, D2),
    ]
    attempt(bars, {"09:30": [LONG]})
    assert seen == [D1]


def test_pending_bracket_does_not_fill_past_flat_by():
    bars = [bar("16:30", 100, 100.5, 99.5, 100), bar("16:50", 100, 100.5, 99.5, 100)]
    r = attempt(bars, {"16:30": [LONG]})
    assert r.fills == ()
    assert r.outcome != "breached"


def test_open_position_exits_when_data_skips_past_flat_by():
    bars = [
        bar("16:30", 100, 100.5, 99.5, 100),
        bar("16:31", 100.5, 101, 100.25, 100.5),  # entry 100.75; data then skips 16:32-16:49
        bar("16:50", 101, 101.5, 100.5, 101),  # exit at open - tick before any mark
    ]
    r = attempt(bars, {"16:30": [LONG]})
    assert [(f.qty, f.price, f.ts) for f in r.fills] == [
        (1, 100.75, et(D1, "16:31")),
        (-1, 100.75, et(D1, "16:50")),
    ]
    assert (r.outcome, r.breach) == ("no_result", None)


def test_pending_bracket_does_not_survive_the_day_roll():
    r = attempt([bar("15:00", 100, 100.5, 99.5, 100), *winning_day(D2)], {"15:00": [LONG]})
    assert r.fills == ()
