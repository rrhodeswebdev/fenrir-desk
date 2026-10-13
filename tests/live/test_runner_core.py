from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from backtest_helpers import D1, NQ, bar, make_rules, rules_data, scripted
from live_helpers import decide_now, make_setup, reconciled

from fenrir.backtest.attempt import MINUTE, run_attempt
from fenrir.core.actions import Bracket, Cancel, Flatten
from fenrir.core.instruments import ContractSpec, in_roll_window
from fenrir.core.sessions import trading_day
from fenrir.live import core
from fenrir.live.core import DayStart, Start
from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import DayClosed
from fenrir.strategy import Strategy

BAR = bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0)
LONG = Bracket("long", 1, 19990.0, 20020.0)  # stop-out: (10 + 1 tick) x $20 = $205


def run(setup, b=BAR, state=None):
    """Start (unless given a state), feed one bar; return the state before decide."""
    s = state or reconciled(core.start(setup, b.ts)[0])
    return core.on_bar(s, b)[0]


def decide(setup, actions, b=BAR):
    return decide_now(run(setup, b), actions)


def kinds(effects):
    return [e.kind if isinstance(e, core.Log) else "send" for e in effects]


def scaled_rules() -> AccountRules:
    d = rules_data()
    d["funded"]["scaling"] = [
        {"from_profit": 0, "max_minis": 2},
        {"from_profit": 1000, "max_minis": 4},
    ]
    return AccountRules.model_validate(d)


def test_dry_bar_logs_bar_and_unsent_bracket():
    s, effects = decide(make_setup(make_rules()), [LONG])
    assert kinds(effects) == ["bar", "action"]
    a = effects[1].payload
    assert (a["type"], a["side"], a["qty"]) == ("Bracket", "long", 1)
    assert (a["sent"], a["mode"]) == (False, "dry")
    assert effects[0].payload["actions"] == 1
    assert s.position.qty == 0  # dry run never fills


def test_ctx_window_is_capped_and_ends_with_the_bar():
    s, _ = core.start(make_setup(make_rules()), BAR.ts)
    for i in range(core.WINDOW + 5):
        last = replace(BAR, ts=BAR.ts + timedelta(minutes=i))
        s, _ = core.on_bar(s, last)
    c = core.ctx(s)
    assert len(c.bars) == core.WINDOW and c.bars[-1] == last and c.ts == last.ts
    assert c.account.drawdown_remaining == 2000.0


def test_over_contract_limit_is_rejected():
    _, effects = decide(make_setup(make_rules()), [replace(LONG, qty=5)])
    assert kinds(effects) == ["bar", "rejection"]
    assert "max_minis 4" in effects[1].payload["reason"]
    assert effects[1].payload["action"]["qty"] == 5


def test_funded_scaling_limits_size_and_status():
    low = Start(phase_start=50_000.0, peak=50_500.0, balance=50_500.0)  # +500: tier 2
    setup = make_setup(
        scaled_rules(),
        mode="sim",
        phase="funded",
        start=low,
        day_start=DayStart(50_500.0, 50_500.0),
    )
    s = run(setup)
    assert core.account_status(s)["limits"]["max_minis"] == 2
    _, effects = decide_now(s, [replace(LONG, qty=3)])
    assert "max_minis 2" in effects[1].payload["reason"]

    high = Start(phase_start=50_000.0, peak=51_500.0, balance=51_500.0)  # +1500: tier 4
    s = run(replace(setup, start=high, day_start=DayStart(51_500.0, 51_500.0)))
    assert core.account_status(s)["limits"]["max_minis"] == 4
    assert kinds(decide_now(s, [replace(LONG, qty=3)])[1]) == ["bar", "action", "send"]

    # the tier comes from today's opening balance, not a winning morning since
    s = run(replace(setup, start=high, day_start=DayStart(50_500.0, 50_500.0)))
    assert core.account_status(s)["limits"]["max_minis"] == 2


def test_stop_out_past_drawdown_is_rejected_from_ninjatrader_start():
    # peak +1000 → floor -1000; balance +500 → $1,500 of room, so a $1,505 stop-out breaches
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)
    wide = Bracket("long", 1, 19925.0, 20020.0)
    setup = make_setup(
        make_rules(), mode="sim", start=start, day_start=DayStart(50_500.0, 51_000.0)
    )
    _, effects = decide(setup, [wide])
    assert kinds(effects) == ["bar", "rejection"]  # journalled, nothing sent (SC5)
    assert "drawdown floor -1000.00" in effects[1].payload["reason"]
    # the same bracket on a fresh account has $2,000 of room
    assert kinds(decide(make_setup(make_rules()), [wide])[1]) == ["bar", "action"]


@pytest.mark.parametrize("mode", ["sim", "live"])
def test_without_day_start_sim_and_live_take_no_brackets(mode):
    start = Start(phase_start=50_000.0, peak=50_000.0, balance=50_000.0)
    day = DayStart(balance=50_000.0, peak=50_000.0)
    setup = make_setup(make_rules(), mode=mode, start=start)
    _, effects = decide(setup, [LONG, Flatten(), Cancel("x")])
    assert kinds(effects) == ["bar", "rejection", "action", "action"]  # flat: nothing sent
    assert effects[1].payload["reason"] == "today's opening balance unknown"
    # the open's figures without the phase start and peak: still no brackets
    _, effects = decide(replace(setup, start=None, day_start=day), [LONG])
    assert kinds(effects) == ["bar", "rejection"] and "DEC-4a" in effects[1].payload["reason"]
    assert kinds(decide(replace(setup, day_start=day), [LONG])[1]) == ["bar", "action", "send"]
    # dry runs start fresh and need no opening balance
    assert kinds(decide(make_setup(make_rules()), [LONG])[1]) == ["bar", "action"]


def test_day_start_sets_the_daily_loss_room():
    # down $350 today (above the −$400 safety level): a $205 stop-out makes the day −$555
    start = Start(phase_start=50_000.0, peak=50_000.0, balance=49_650.0)
    setup = make_setup(
        make_rules(daily_loss=500.0),
        mode="sim",
        start=start,
        day_start=DayStart(50_000.0, 50_000.0),
    )
    s = run(setup)
    assert (s.acct.day_start_balance, s.session_start) == (0.0, 0.0)
    _, effects = decide_now(s, [LONG])
    assert "daily loss limit" in effects[1].payload["reason"]


def test_stop_out_past_daily_loss_limit_is_rejected():
    rules = make_rules(daily_loss=500.0)
    _, effects = decide(make_setup(rules), [Bracket("long", 1, 19975.0, 20020.0)])  # $505
    assert kinds(effects) == ["bar", "rejection"]
    assert "daily loss limit" in effects[1].payload["reason"]
    assert kinds(decide(make_setup(rules), [Bracket("long", 1, 19976.0, 20020.0)])[1]) == [
        "bar",
        "action",
    ]


def test_no_entries_at_or_after_flat_time():
    late = bar("16:39", 20000.0, 20005.0, 19995.0, 20000.0)  # closes 16:40 ET
    _, effects = decide(make_setup(make_rules()), [LONG, Flatten()], late)
    assert kinds(effects) == ["bar", "rejection", "action"]  # exits still allowed
    assert "16:40" in effects[1].payload["reason"]
    early = bar("16:38", 20000.0, 20005.0, 19995.0, 20000.0)
    assert kinds(decide(make_setup(make_rules()), [LONG], early)[1]) == ["bar", "action"]


def test_roll_window_bar_is_journalled_but_never_shown_to_step():
    # NQ Z25 -> H26: trading days 2025-12-11 .. 2025-12-16 are the roll window
    s = run(make_setup(make_rules()))
    inside = bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0, date(2025, 12, 16))
    assert not core.runs_step(inside)
    held, effects = core.on_bar(s, inside)
    assert held.bars == s.bars and held.last_seen == inside.ts + MINUTE
    assert effects[-1] == core.Log("bar", {**asdict(inside), "actions": 0})
    after = bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0, date(2025, 12, 17))
    assert core.runs_step(after)
    assert core.ctx(core.on_bar(held, after)[0]).bars == (*s.bars, after)


def test_roll_window_starts_and_ends_at_18_et():
    # trading days start at 18:00 ET (23:00 UTC in December), as load_bars counts them
    assert not core.runs_step(bar("18:30", 1.0, 1.0, 1.0, 1.0, date(2025, 12, 10)))  # Thu's
    assert core.runs_step(bar("16:59", 1.0, 1.0, 1.0, 1.0, date(2025, 12, 10)))
    assert core.runs_step(bar("18:30", 1.0, 1.0, 1.0, 1.0, date(2025, 12, 16)))  # Wed's


def test_decide_takes_no_bracket_unless_the_last_bar_is_the_current_one():
    s = run(
        make_setup(make_rules()),
        bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0, date(2025, 12, 10)),
    )
    s = core.on_bar(s, bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0, date(2025, 12, 16)))[0]
    _, effects = decide_now(s, [LONG])
    assert kinds(effects) == ["bar", "rejection"]
    assert effects[1].payload["reason"] == "not the latest bar: no entry at an older bar's close"


def test_step_sees_the_bars_the_backtest_shows_it_around_a_roll():
    days = (date(2025, 12, 10), date(2025, 12, 11), date(2025, 12, 12), date(2025, 12, 17))
    bars = tuple(
        bar(hhmm, 20000.0, 20005.0, 19995.0, 20000.0 + i, d)
        for i, d in enumerate(days)
        for hhmm in ("09:30", "09:31", "09:32")
    )
    seen = []

    def record(state, b, ctx):
        seen.append(ctx.bars)
        return state, []

    loaded = tuple(b for b in bars if not in_roll_window(trading_day(b.ts)))  # as load_bars
    strategy = Strategy("rec", Path("rec.py"), "0" * 64, "NQ", {}, lambda p: None, record)
    run_attempt(loaded, 0, len(loaded), strategy, make_rules(), NQ)
    s, live = core.start(make_setup(make_rules()), bars[0].ts)[0], []
    for b in bars:  # as LiveHub._bar
        s, _ = core.on_bar(s, b)
        if core.runs_step(b):
            live.append(core.ctx(s).bars)
    assert len(live) == 6 and live == [tuple(c) for c in seen]


def test_one_bracket_per_bar_and_flatten_cancel_replace_it():
    _, effects = decide(make_setup(make_rules()), [LONG, LONG, Cancel("x"), LONG])
    assert kinds(effects) == ["bar", "rejection", "rejection", "action", "action"]
    assert effects[1].payload["reason"] == "replaced by a Cancel this bar"
    assert effects[2].payload["reason"] == "a bracket was already accepted this bar"


def test_halted_account_rejects_brackets():
    s, effects = core.halt(run(make_setup(make_rules())), "test")
    assert kinds(effects) == ["halt"]
    _, effects = decide_now(s, [LONG])
    assert effects[1].payload["reason"] == "halted: test"


def test_strategy_error_halts_account():
    s, effects = core.step_failed(run(make_setup(make_rules())), "RuntimeError('bad index')")
    assert kinds(effects) == ["bar", "halt"]
    assert "bad index" in s.halted and s.halt_day is None
    assert kinds(decide_now(s, [LONG])[1]) == ["bar", "rejection"]


def test_changed_strategy_file_halts_at_start():
    s, effects = core.start(make_setup(make_rules(), strategy_hash="h2"), BAR.ts)
    assert kinds(effects) == ["halt"] and "changed since arming" in s.halted
    assert kinds(decide_now(run(None, state=s), [LONG])[1]) == ["bar", "rejection"]


@pytest.mark.parametrize(
    ("now", "reason"),
    [
        ({"rules_hash": "r2"}, "rules file changed since arming; re-arm"),
        ({"instruments_hash": "i2"}, "instruments.yaml changed since arming; re-arm"),
        (
            {"dll_on": True},
            "dll_on changed since arming (armed off, accounts.yaml now on); re-arm",
        ),
    ],
    ids=str,
)
@pytest.mark.parametrize("mode", ["dry", "sim"])
def test_a_pinned_input_that_changed_halts_at_start(now, reason, mode):
    """DEC-4d: the runner never trades rules, instruments or a DLL choice it was not armed with."""
    start = Start(phase_start=50_000.0, peak=50_000.0, balance=50_000.0)
    s, effects = core.start(make_setup(make_rules(), mode=mode, start=start, **now), BAR.ts)
    assert kinds(effects) == ["halt"] and s.halted == reason
    assert effects[0].payload == {"reason": reason, "kind": None, "until": None}
    assert core.account_status(s)["balance"] is None  # cannot start: no figures
    assert kinds(decide_now(run(None, state=s), [LONG])[1]) == ["bar", "rejection"]


def test_a_row_armed_before_the_pins_halts_at_start():
    setup = make_setup(make_rules())
    legacy = replace(setup.armed, dll_on=None, rules_hash=None, instruments_hash=None)
    s, _ = core.start(replace(setup, armed=legacy), BAR.ts)
    assert (
        s.halted
        == "armed before dll_on and the rules and instruments.yaml hashes were pinned; re-arm"
    )
    assert not s.started


def test_sim_without_start_balances_halts_and_dry_ignores_them():
    s, effects = core.start(make_setup(make_rules(), mode="sim"), BAR.ts)
    assert kinds(effects) == ["halt"] and "DEC-4a" in s.halted
    st = core.account_status(s)  # cannot start: no figures (Plans 21–24 AccountStatus)
    assert st["halted"] and "DEC-4a" in st["halt_reason"]
    assert [st[k] for k in ("balance", "today_pnl", "limits", "position", "session")] == [None] * 5
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)
    s, effects = core.start(make_setup(make_rules(), start=start), BAR.ts)
    assert effects == () and (s.acct.balance, s.acct.floor) == (0.0, -2000.0)


@pytest.mark.parametrize("locks_at", [None, 2100.0])
def test_start_floor_matches_the_drawdown_rule(locks_at):
    d = rules_data()
    d["evaluation"]["max_drawdown"]["locks_at"] = locks_at
    rules = AccountRules.model_validate(d)
    start = Start(phase_start=50_000.0, peak=53_000.0, balance=50_200.0)
    acct = core.start_account(rules, "evaluation", D1, start)
    assert (acct.balance, acct.day_start_balance, acct.peak) == (200.0, 200.0, 3000.0)
    # the floor the backtest's rule reaches after closing a day at the same peak
    fresh = core.start_account(rules, "evaluation", D1, None)
    trailed = max_drawdown(rules.evaluation.max_drawdown)(
        replace(fresh, balance=3000.0), DayClosed(D1, BAR.ts)
    )
    assert acct.floor == trailed.floor == (100.0 if locks_at else 1000.0)


def test_intraday_start_floor_trails_at_least_the_balance():
    rules = make_rules(trailing="intraday")
    start = Start(phase_start=50_000.0, peak=50_500.0, balance=51_000.0)  # peak entered too low
    acct = core.start_account(rules, "evaluation", D1, start)
    assert (acct.peak, acct.floor) == (1000.0, -1000.0)


@pytest.mark.parametrize(
    ("daily_loss", "level"),
    [(500.0, -400.0), (None, -1600.0)],  # 0 − 0.8 × 500; −2000 + 0.2 × 2000 when the DLL is off
)
def test_safety_margin_halts_for_the_day(daily_loss, level):
    s = run(make_setup(make_rules(daily_loss=daily_loss)))
    assert core.safety_level(s) == level
    near = replace(s, acct=replace(s.acct, balance=level + 1))
    assert core.on_bar(near, BAR)[1] == ()
    s, effects = core.on_bar(replace(s, acct=replace(s.acct, balance=level)), BAR)
    assert kinds(effects) == ["halt"]
    assert (s.halt_kind, s.halt_day) == ("safety", s.acct.day)
    assert s.halted.startswith("safety margin")
    s, effects = core.on_bar(s, BAR)
    assert effects == ()  # one halt, not one per bar
    assert decide_now(s, [LONG])[1][1].kind == "rejection"


def test_safety_drawdown_leg_uses_the_session_start_room():
    # end-of-day trailing: the floor does not move intraday, so the open's floor is today's
    start = Start(phase_start=50_000.0, peak=51_000.0, balance=50_500.0)  # room 1500
    setup = make_setup(make_rules(), mode="sim", start=start)
    assert core.safety_level(run(setup)) == pytest.approx(-1000.0 + 0.2 * 1500)
    s = run(replace(setup, day_start=DayStart(balance=50_500.0, peak=51_000.0)))
    assert core.safety_level(s) == pytest.approx(-700.0)


@pytest.mark.parametrize(
    ("peak", "balance", "level"),
    [(50_800.0, 50_100.0, -800.0), (52_300.0, 50_400.0, 700.0)],  # floor now + 0.2 × 2000
)
def test_intraday_safety_room_is_measured_at_the_open(peak, balance, level):
    # day opened at +0 with peak +0 (floor −2000); the morning ran up to `peak` before a restart
    start = Start(phase_start=50_000.0, peak=peak, balance=balance)
    day = DayStart(balance=50_000.0, peak=50_000.0)
    s = run(make_setup(make_rules(trailing="intraday"), mode="sim", start=start, day_start=day))
    assert (s.session_start, s.session_floor) == (0.0, -2000.0)
    assert core.safety_level(s) == pytest.approx(level)


def test_account_status_shape():
    st = core.account_status(run(make_setup(make_rules())))
    assert set(st) == {
        "account", "mode", "phase", "strategy", "rules_account", "contract", "halted",
        "halt_kind", "halt_reason", "balance", "nt_balance", "today_pnl", "limits", "position",
        "session", "conditions",
    }  # fmt: skip
    assert st["nt_balance"] is None  # no NinjaTrader figures (always for dry runs)
    assert st["limits"] == {
        "drawdown_remaining": 2000.0,
        "drawdown_max": 2000.0,
        "daily_loss_remaining": None,
        "daily_loss_max": None,
        "max_minis": 4,
        "held_minis": 0.0,
    }
    assert st["session"] == {
        "start": 0.0,
        "floor": -2000.0,
        "target": 3000.0,
        "safety": -1600.0,
        "equity": [[BAR.ts.isoformat(), 0.0]],
        "fills": [],
    }
    assert (st["strategy"], st["halted"], st["halt_kind"], st["position"]["qty"]) == (
        "orb",
        False,
        None,
        0,
    )


MNQ = ContractSpec("MNQ", "NQ", 2.0, 0.25, 0.1)
NAN = float("nan")


def live_brackets(effects):
    return [
        e.payload["qty"] for e in effects if e.kind == "action" and e.payload["type"] == "Bracket"
    ]


def backtest_brackets(bars, script, rules, spec, first_fill=0):
    """Qty of the bracket the backtest opens from `script`, as a 0- or 1-item list."""
    result = run_attempt(bars, 0, len(bars), scripted(script), rules, spec)
    return [abs(f.qty) for f in result.fills[first_fill : first_fill + 1]]


@pytest.mark.parametrize(
    ("hhmm", "actions", "daily_loss", "spec"),
    [
        ("09:30", [LONG], None, NQ),
        ("09:30", [replace(LONG, qty=5)], None, NQ),  # contract limit
        ("09:30", [replace(LONG, qty=NAN)], None, NQ),
        ("09:30", [replace(LONG, qty=1.5)], None, NQ),
        ("09:30", [Bracket("long", 1, 19900.0, 20020.0)], None, NQ),  # drawdown
        ("09:30", [Bracket("long", 1, 19901.0, 20020.0)], None, NQ),
        ("09:30", [Bracket("long", 1, 19975.0, 20020.0)], 500.0, NQ),  # daily loss limit
        ("09:30", [Bracket("short", 1, 20010.0, 19980.0)], 500.0, NQ),
        ("09:30", [Bracket("long", 1, 20010.0, 20020.0)], None, NQ),  # stop on the wrong side
        ("16:39", [LONG], None, NQ),  # flat time
        ("09:30", [LONG, replace(LONG, qty=2)], None, NQ),  # first bracket wins
        ("09:30", [LONG, Flatten(), replace(LONG, qty=2)], None, NQ),  # last bracket wins
        ("09:30", [LONG, Cancel("x")], None, NQ),
        ("09:30", [replace(LONG, qty=40)], None, MNQ),  # 4 mini-equivalents
        ("09:30", [replace(LONG, qty=41)], None, MNQ),
        ("09:30", [Bracket("long", 10, 19900.0, 20020.0)], None, MNQ),  # $2,005 stop-out
        ("09:30", [Bracket("long", 10, 19901.0, 20020.0)], None, MNQ),
    ],
)
def test_accepts_exactly_what_the_backtest_accepts(hhmm, actions, daily_loss, spec):
    rules = make_rules(daily_loss=daily_loss)
    b = bar(hhmm, 20000.0, 20005.0, 19995.0, 20000.0)
    following = replace(b, ts=b.ts + timedelta(minutes=1))
    expected = backtest_brackets((b, following), {hhmm: actions}, rules, spec)
    _, effects = decide(make_setup(rules, spec=spec), actions, b)
    assert live_brackets(effects) == expected


@pytest.mark.parametrize(("stop", "accepted"), [(19976.0, True), (19975.0, False)])
def test_after_a_loss_accepts_exactly_what_the_backtest_accepts(stop, accepted):
    # 09:30 long stops out at 09:32: (20000.25 − 19924.75) × $20 = $1,510 lost, $490 of room left
    rules = make_rules()
    bars = (
        bar("09:30", 20000.0, 20005.0, 19995.0, 20000.0),
        bar("09:31", 20000.0, 20005.0, 19995.0, 20000.0),
        bar("09:32", 20000.0, 20005.0, 19900.0, 19950.0),
        bar("09:33", 20000.0, 20005.0, 19995.0, 20000.0),
        bar("09:34", 20000.0, 20005.0, 19995.0, 20000.0),
    )
    late = Bracket("long", 1, stop, 20020.0)  # $485 or $505 stop-out
    script = {"09:30": [Bracket("long", 1, 19925.0, 20100.0)], "09:33": [late]}
    expected = backtest_brackets(bars, script, rules, NQ, first_fill=2)
    s = run(make_setup(rules), bars[3])
    s = replace(s, acct=replace(s.acct, balance=-1510.0, traded_today=True))
    assert live_brackets(decide_now(s, [late])[1]) == expected == ([1] if accepted else [])
