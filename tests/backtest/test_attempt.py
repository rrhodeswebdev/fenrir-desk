from backtest_helpers import NQ, bar, make_rules, scripted

from fenrir.backtest.attempt import run_attempt
from fenrir.core.actions import Bracket, Cancel, Flatten

LONG = Bracket("long", 1, 95.0, 105.0)
DECIDE = bar("09:30", 100, 100.5, 99.5, 100)  # strategy acts on this bar's close (100)


def attempt(bars, script, **rules_kw):
    return run_attempt(tuple(bars), 0, len(bars), scripted(script), make_rules(**rules_kw), NQ)


def fills(result):
    return [(f.qty, f.price) for f in result.fills]


def test_long_hits_target():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 105.5, 100.5, 105)],
        {"09:30": [LONG]},
    )
    assert fills(r) == [(1, 100.75), (-1, 105.0)]  # entry: next open + 1 tick; target at limit
    assert r.fills[1].realized_pnl == (105.0 - 100.75) * 20  # 85.0
    assert r.fills[0].realized_pnl == 0.0
    assert (r.outcome, r.breach, r.microscalp_flags) == ("no_result", None, 0)


def test_commission_per_contract_per_side():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 105.5, 100.5, 105)],
        {"09:30": [Bracket("long", 2, 95.0, 105.0)]},
        commission=2.5,
    )
    assert [f.commission for f in r.fills] == [5.0, 5.0]


def test_short_hits_target():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 101, 100, 100.5), bar("09:32", 100, 100.5, 94.5, 95.5)],
        {"09:30": [Bracket("short", 1, 105.0, 95.0)]},
    )
    assert fills(r) == [(-1, 100.25), (1, 95.0)]
    assert r.fills[1].realized_pnl == (100.25 - 95.0) * 20  # 105.0


def test_stop_first_when_both_inside_one_bar():
    r = attempt([DECIDE, bar("09:31", 100.5, 106, 94, 100)], {"09:30": [LONG]})
    assert fills(r) == [(1, 100.75), (-1, 94.75)]  # stop 95 - 1 tick, same bar as entry
    assert r.fills[1].realized_pnl == (94.75 - 100.75) * 20  # -120.0
    assert r.microscalp_flags == 1


def test_gap_through_stop_fills_at_open():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 101, 100, 100.5), bar("09:32", 94, 94.5, 93, 94)],
        {"09:30": [LONG]},
    )
    assert fills(r) == [(1, 100.75), (-1, 93.75)]


def test_flatten_exits_at_next_open():
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101), bar("09:32", 101, 101.5, 100.5, 101)],
        {"09:30": [LONG], "09:31": [Flatten()]},
    )
    assert fills(r) == [(1, 100.75), (-1, 100.75)]  # 101 open - 1 tick


def test_cancel_drops_pending_entry():
    r = attempt([DECIDE, bar("09:31", 100.5, 102, 100, 101)], {"09:30": [LONG, Cancel("any")]})
    assert r.fills == ()


def test_invalid_bracket_is_ignored():
    bars = [DECIDE, bar("09:31", 100.5, 102, 100, 101)]
    assert (
        attempt(bars, {"09:30": [Bracket("long", 1, 101.0, 105.0)]}).fills == ()
    )  # stop above price
    assert (
        attempt(bars, {"09:30": [Bracket("short", 1, 95.0, 90.0)]}).fills == ()
    )  # stop below price
    assert attempt(bars, {"09:30": [Bracket("long", 0, 95.0, 105.0)]}).fills == ()


def test_one_position_at_a_time():
    r = attempt(
        [
            DECIDE,
            bar("09:31", 100.5, 102, 100, 101),
            bar("09:32", 101, 105.5, 100.5, 105),
            bar("09:33", 105, 106, 104.5, 105),
        ],
        {"09:30": [LONG, LONG], "09:31": [LONG]},
    )
    assert fills(r) == [(1, 100.75), (-1, 105.0)]


def test_pretrade_check_rejects_bracket_that_could_breach():
    # stop-out: (100 - 90 + 0.25) * 20 = 205 > 100 drawdown
    r = attempt(
        [DECIDE, bar("09:31", 100.5, 102, 100, 101)],
        {"09:30": [Bracket("long", 1, 90.0, 105.0)]},
        drawdown=100.0,
    )
    assert r.fills == ()


def test_drawdown_breach_ends_attempt():
    # pre-trade allows it (stop-out 25), but the gap fills at 93.75: -140 < -100
    r = attempt(
        [
            DECIDE,
            bar("09:31", 100.5, 101, 100, 100.5),
            bar("09:32", 94, 94.5, 93, 94),
            bar("09:33", 94, 99, 94, 98),
        ],
        {"09:30": [Bracket("long", 1, 99.0, 105.0)], "09:33": [LONG]},
        drawdown=100.0,
    )
    assert r.outcome == "breached"
    assert r.breach.rule == "max_drawdown"
    assert fills(r) == [(1, 100.75), (-1, 93.75)]
    assert all(f.ts <= r.breach.ts for f in r.fills)


def test_data_ends_in_position():
    r = attempt([DECIDE, bar("09:31", 100.5, 102, 100, 101)], {"09:30": [LONG]})
    assert fills(r) == [(1, 100.75)]
    assert r.outcome == "no_result"


def test_daily_loss_limit_flattens_and_halts_the_day():
    # pre-trade allows it at 100 (stop-out 65 <= 100 DLL), but the entry gaps to 102.25:
    # the stop now risks 105
    dll_long = Bracket("long", 1, 97.0, 105.0)
    r = attempt(
        [
            DECIDE,
            bar("09:31", 102, 102.5, 101.5, 102),
            bar(
                "09:32", 101, 101, 97.25, 97.5
            ),  # worst (97.25 - 102.25) * 20 = -100: DLL hit, stop 97 untouched
            bar("09:33", 97.5, 98, 97.25, 97.5),
            bar("09:34", 97.5, 99, 97.25, 98.5),
        ],
        {"09:30": [dll_long], "09:33": [dll_long]},
        daily_loss=100.0,
    )
    assert fills(r) == [
        (1, 102.25),
        (-1, 97.0),
    ]  # flattened at the limit 97.25 - 1 tick; 09:33 entry refused
    assert (r.outcome, r.breach) == ("no_result", None)  # a day halt, not a failure


def test_daily_loss_limit_exit_slippage_can_breach_drawdown():
    # DLL hit at -100 (drawdown 104 not touched); the exit one tick lower realizes -105 <= -104
    dll_long = Bracket("long", 1, 97.0, 105.0)
    r = attempt(
        [
            DECIDE,
            bar("09:31", 102, 102.5, 101.5, 102),
            bar("09:32", 101, 101, 97.25, 97.5),
        ],
        {"09:30": [dll_long]},
        daily_loss=100.0,
        drawdown=104.0,
    )
    assert (r.outcome, r.breach.rule, r.breach.ts) == (
        "breached",
        "max_drawdown",
        bar("09:32", 0, 0, 0, 0).ts,
    )


def test_target_bar_low_is_checked_against_the_daily_loss_limit():
    # entry gaps to 102.25; the target bar's low 97.25 is worth -100 (stop 96 untouched):
    # the low may have come first, so the DLL halts the day whether or not the high reached 105
    dll_long = Bracket("long", 1, 96.0, 105.0)
    for high in (105.5, 104.5):
        r = attempt(
            [DECIDE, bar("09:31", 102, 102.5, 101.5, 102), bar("09:32", 102, high, 97.25, 104)],
            {"09:30": [dll_long]},
            daily_loss=100.0,
        )
        assert fills(r) == [(1, 102.25), (-1, 97.0)]  # the DLL level 97.25 - 1 tick
        assert (r.outcome, r.breach) == ("no_result", None)


def test_target_bar_low_is_checked_against_an_intraday_floor():
    # 09:31 high 105.5 trails the floor to 95.5, above the stop 95; 09:32 dips to 95.25
    # before hitting the target 108: live that breaches first
    r = attempt(
        [
            DECIDE,
            bar("09:31", 100.5, 105.5, 100.25, 105),
            bar("09:32", 105, 108.5, 95.25, 108),
        ],
        {"09:30": [Bracket("long", 1, 95.0, 108.0)]},
        drawdown=200.0,
        trailing="intraday",
    )
    assert (r.outcome, r.breach.rule) == ("breached", "max_drawdown")
    assert fills(r) == [(1, 100.75)]


def test_flat_by_bar_low_is_checked_against_the_daily_loss_limit():
    # the 16:39 bar closes at flat-by (16:40); its low 96.75 is worth -80 (stop 96.5 untouched)
    r = attempt(
        [
            bar("16:30", 100, 100.5, 99.5, 100),
            bar("16:31", 100.5, 101, 100.25, 100.5),
            bar("16:39", 100.5, 101, 96.75, 100.5),
        ],
        {"16:30": [Bracket("long", 1, 96.5, 105.0)]},
        daily_loss=80.0,
    )
    assert fills(r) == [(1, 100.75), (-1, 96.5)]  # DLL level 96.75 - 1 tick, not the 100.25 close
