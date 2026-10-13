"""22.3: link loss, reconciliation with NinjaTrader, resolve and the restart replay."""

from dataclasses import replace
from datetime import timedelta

import pytest
from backtest_helpers import D1, D2, bar, make_rules
from live_helpers import INSTRUMENT, decide_now, fill, make_setup, reconciled

from fenrir.bridge.reconcile import NtOrder, NtPosition
from fenrir.core.actions import Bracket, Flatten
from fenrir.live import core
from fenrir.live.core import DayStart, Log, Send, Start

LONG = Bracket("long", 1, 19990.0, 20020.0)
START = Start(phase_start=50_000.0, peak=50_000.0, balance=50_000.0)
OPEN = DayStart(balance=50_000.0, peak=50_000.0)


def at(hhmm, day=D1, c=20000.0):
    return bar(hhmm, 20000.0, 20005.0, 19995.0, c, day)


def sim(**kw):
    return make_setup(make_rules(**kw), mode="sim", start=START, day_start=OPEN)


def ready(setup, b):
    """Started, reconciled with a flat NinjaTrader book, then fed bar `b`."""
    return core.on_bar(reconciled(core.start(setup, b.ts)[0]), b)[0]


def entered(setup, b):
    """Long LONG from bar `b`, its entry filled at the close 1 s into the next bar."""
    s, _ = decide_now(ready(setup, b), [LONG])
    return core.on_fill(s, fill(1, 20000.0, b.ts + timedelta(seconds=61), cid=s.working.client_id))[
        0
    ]


def kinds(effects):
    return [e.kind if isinstance(e, Log) else "send" for e in effects]


def sends(effects):
    return [e for e in effects if isinstance(e, Send)]


def halts(effects):
    return [e.payload for e in effects if isinstance(e, Log) and e.kind == "halt"]


def held(qty, avg=20000.0):
    return NtPosition("Sim101", INSTRUMENT, qty, avg)


def order(role, action, qty, price, cid="c1", oid="o1"):
    return NtOrder("Sim101", oid, cid, INSTRUMENT, role, action, qty, price, "working")


def reason(s, actions=(LONG,)):
    """Why the current bar's LONG is rejected, or None when it goes out."""
    effects = decide_now(s, list(actions))[1]
    return effects[1].payload["reason"] if effects[1].kind == "rejection" else None


def test_sim_and_live_start_paused_until_reconciled():
    b = at("09:30")
    s = core.on_bar(core.start(sim(), b.ts)[0], b)[0]
    assert reason(s).startswith("paused: not reconciled with NinjaTrader")
    st = core.account_status(s)  # the brief pause shows only as the reason
    assert (st["halted"], st["halt_kind"]) == (False, None) and "reconciled" in st["halt_reason"]
    s, effects = core.on_reconcile(s, INSTRUMENT, (), (), b.ts)
    assert effects == () and s.unreconciled is None and reason(s) is None
    dry = core.on_bar(core.start(make_setup(make_rules()), b.ts)[0], b)[0]
    assert reason(dry) is None and core.on_disconnect(dry, "x") == (dry, ())


def test_one_alert_per_outage():
    s = ready(sim(), at("09:30"))
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")
    assert kinds(effects) == ["alert"] and "no heartbeat for 6 s" in effects[0].payload["message"]
    assert reason(s).startswith("paused: NinjaTrader link lost")
    s, effects = core.on_disconnect(s, "connection refused")  # the stream retries
    assert effects == ()
    s, _ = core.on_reconcile(s, INSTRUMENT, (), (), at("09:31").ts)
    assert reason(s) is None
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")  # a new outage
    assert kinds(effects) == ["alert"]


def test_a_mismatch_pauses_until_resolved():
    s = ready(sim(), at("09:30"))
    stray = order("other", "buy", 1, 19900.0)
    s, effects = core.on_reconcile(s, INSTRUMENT, (), [stray], at("09:31").ts)
    [row] = halts(effects)
    assert row["kind"] == "reconcile" and "stray_orders" in row["reason"]
    assert reason(s).startswith("paused: NinjaTrader mismatch: stray_orders")
    st = core.account_status(s)
    assert (st["halted"], st["halt_kind"]) == (True, "reconcile")
    assert core.on_reconcile(s, INSTRUMENT, (), [stray], at("09:31").ts)[1] == ()  # once
    # the user cancelled the order and the link came back: still paused until they resolve
    s, _ = core.on_reconcile(s, INSTRUMENT, (), (), at("09:32").ts)
    assert reason(s).startswith("paused: NinjaTrader mismatch")
    s, effects = core.resolve(s, INSTRUMENT, (), (), at("09:32").ts)
    assert [r["kind"] for r in halts(effects)] == ["resolve"]
    assert reason(s) is None and core.account_status(s)["halted"] is False


def test_resolve_with_the_mismatch_still_there_pauses_again():
    s = ready(sim(), at("09:30"))
    stray = order("other", "buy", 1, 19900.0)
    s, _ = core.on_reconcile(s, INSTRUMENT, (), [stray], at("09:31").ts)
    s, effects = core.resolve(s, INSTRUMENT, (), [stray], at("09:32").ts)
    assert [r["kind"] for r in halts(effects)] == ["resolve", "reconcile"]
    assert reason(s).startswith("paused: NinjaTrader mismatch")


def test_resolve_clears_reconcile_and_drift_never_a_breach_and_survives_restart():
    b = at("16:30")
    s, journal = ready(sim(), b), []
    s, effects = decide_now(s, [LONG])
    journal += effects
    far = fill(1, 20002.0, b.ts + timedelta(seconds=61), cid=s.working.client_id)  # 8 ticks off
    s, effects = core.on_fill(s, far)
    journal += effects
    assert [r["kind"] for r in halts(effects)] == ["drift"]
    s, effects = core.on_bar(s, at("09:30", D2))  # still open at the day roll, while reconciled
    journal += effects
    assert s.halt_kind == "breach"
    s, effects = core.on_reconcile(s, INSTRUMENT, (), (), at("09:31", D2).ts)  # NT is flat
    journal += effects
    assert s.mismatch and s.drift
    restarted = core.start(sim(), at("09:32", D2).ts, halts(journal))[0]
    assert (restarted.mismatch, restarted.drift) == (s.mismatch, s.drift)
    s, effects = core.resolve(s, INSTRUMENT, (), (), at("09:32", D2).ts)
    journal += effects
    assert (s.mismatch, s.drift, s.halt_kind) == (None, None, "breach")
    assert reason(s).startswith("halted: breach flat_by")
    restarted = core.start(sim(), at("09:33", D2).ts, halts(journal))[0]
    assert (restarted.mismatch, restarted.drift) == (None, None)
    assert (restarted.halted, restarted.halt_kind) == (s.halted, "breach")


def test_reconcile_adopts_ninjatraders_position_and_flattens_it_without_a_stop():
    s = ready(sim(), at("09:30"))  # flat, but a manual long 2 is open
    now = at("09:31").ts
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(2, 20001.0)], (), now)
    assert s.position == core.Position(2, 20001.0, None, None) and s.acct.position == 2
    assert "position" in s.mismatch and "missing_stop" in s.mismatch
    assert kinds(effects) == ["halt", "alert", "action", "send"]
    assert [e.action for e in sends(effects)] == [Flatten()]
    assert s.exiting == sends(effects)[0].client_id
    # protected in NinjaTrader: adopted with its worst stop, and not flattened
    oco = [
        order("stop", "sell", 1, 19985.0),
        order("stop", "sell", 1, 19980.0, oid="o2"),
        order("target", "sell", 2, 20020.0, oid="o3"),
    ]
    s = ready(sim(), at("09:30"))
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(2, 20001.0)], oco, now)
    assert s.position == core.Position(2, 20001.0, 19980.0, 20020.0)
    assert kinds(effects) == ["halt"] and s.mismatch.startswith("NinjaTrader mismatch: position")


def test_a_fill_past_flat_by_while_unreconciled_is_still_a_breach():
    s = core.on_bar(core.start(sim(), at("16:50").ts)[0], at("16:50"))[0]
    s, _ = core.on_fill(s, fill(1, 20000.0, at("16:51").ts))  # NinjaTrader's own report
    assert s.halt_kind == "breach" and s.acct.position == 1


def test_held_qty_above_the_oco_qty_is_flagged_and_flattened():
    s = entered(sim(), at("09:30"))
    # a manual buy adds to the bracketed long: the add-on resizes the OCO only for entry fills
    s, effects = core.on_fill(s, fill(1, 20001.0, at("09:32").ts))
    assert s.position.qty == 2 and s.position.stop == 19990.0 and sends(effects) == []
    oco = [order("stop", "sell", 1, 19990.0), order("target", "sell", 1, 20020.0, oid="o2")]
    now = at("09:33").ts
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(2, 20000.5)], oco, now)
    assert "missing_stop: stop orders cover 1 of 2" in s.mismatch
    assert s.position == core.Position(2, 20000.5, None, None)  # the runner's own average
    assert [e.action for e in sends(effects)] == [Flatten()]


def test_a_clean_reconcile_takes_the_stop_and_target_from_ninjatrader():
    s = entered(sim(), at("09:30"))
    oco = [order("stop", "sell", 1, 19985.0), order("target", "sell", 1, 20020.0, oid="o2")]
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(1, 20000.25)], oco, at("09:31").ts)
    assert effects == () and s.mismatch is None  # the user moved the stop in NinjaTrader
    assert s.position == core.Position(1, 20000.0, 19985.0, 20020.0)  # its own entry kept


def test_reconcile_frees_an_entry_ninjatrader_rejected_after_accepting():
    s, _ = decide_now(ready(sim(), at("09:30")), [LONG])
    cid = s.working.client_id
    # still working in NinjaTrader: kept, and not a stray order
    entry = order("entry", "buy", 1, 0.0, cid=cid)
    kept, effects = core.on_reconcile(s, INSTRUMENT, (), [entry], at("09:31").ts)
    assert effects == () and kept.working == s.working
    # rejected after the add-on accepted it: no fill will come
    s, effects = core.on_reconcile(s, INSTRUMENT, (), (), at("09:31").ts)
    assert effects == () and s.working is None
    s = core.on_bar(s, at("09:31"))[0]
    assert reason(s) is None


def test_reconcile_forgets_a_flatten_in_flight():
    s = core.on_bar(entered(sim(), at("09:30")), at("09:31"))[0]
    s, _ = decide_now(s, [Flatten()])
    assert s.exiting
    s, _ = core.on_reconcile(s, INSTRUMENT, (), (), at("09:32").ts)  # the Flatten filled
    assert (s.exiting, s.position, s.acct.position, s.opened) == (None, core.FLAT, 0, None)
    assert "position: runner expects 1" in s.mismatch  # its fill was missed


def test_unreconciled_position_at_flat_by_and_the_roll_pauses_instead_of_breaching():
    # intraday: an end_of_day roll with the link down would halt for its closing balance
    s = entered(sim(trailing="intraday"), at("16:30"))
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")  # the add-on flattens at 16:40 unseen
    for hhmm in ("16:39", "16:45"):
        s, _ = core.on_bar(s, at(hhmm))
    s, effects = core.on_bar(s, at("09:30", D2))
    assert s.halted is None and s.acct.day == D2 and s.acct.position == 1
    assert [r["kind"] for r in halts(effects)] == ["reconcile"]
    assert "still open at day close" in s.mismatch
    assert core.account_status(s)["halt_kind"] == "reconcile"
    s, _ = core.on_reconcile(s, INSTRUMENT, (), (), at("09:31", D2).ts)
    assert s.position == core.FLAT
    s, _ = core.resolve(s, INSTRUMENT, (), (), at("09:31", D2).ts)
    s = core.on_bar(s, at("09:31", D2))[0]
    assert s.halted is None and reason(s) is None


def test_replay_rebuilds_position_and_pnl_from_journalled_fills():
    journal = []

    def run(step):
        s, effects = step
        journal.extend((e.kind, e.payload) for e in effects if isinstance(e, Log))
        return s

    def restart(upto, live):
        """Restarted at 09:36 with NinjaTrader's balance holding the live run's realized P&L."""
        nt_now = replace(START, balance=START.balance + live.acct.balance)
        rows = journal[:upto]
        setup = replace(sim(), start=nt_now)
        started = core.start(setup, at("09:36").ts, [p for k, p in rows if k == "halt"])[0]
        return core.replay(started, rows)

    b = at("09:30")
    s = run(decide_now(ready(sim(), b), [Flatten(), LONG]))  # an unsent Flatten row too
    entry = fill(1, 20000.25, b.ts + timedelta(seconds=61), 2.0, s.working.client_id)
    s = run(core.on_fill(s, entry))
    s = run(core.on_bar(s, at("09:31")))
    s = run(core.on_fill(s, fill(-1, 20010.0, at("09:32").ts, 2.0)))  # a manual exit
    s = run(core.on_bar(s, at("09:33")))
    s = run(decide_now(s, [LONG]))
    pending, upto = s, len(journal)
    entry = fill(1, 20000.0, at("09:34").ts + timedelta(seconds=1), 2.0, s.working.client_id)
    s = run(core.on_fill(s, entry))
    s = run(core.on_bar(s, at("09:34")))
    s = run(decide_now(s, [Flatten()]))

    r = restart(len(journal), s)
    assert r.position == s.position == core.Position(1, 20000.0, 19990.0, 20020.0)
    assert r.acct.balance == pytest.approx(s.acct.balance) == pytest.approx(9.75 * 20 - 3 * 2.0)
    assert (r.acct.position, r.acct.traded_today, r.fills, r.opened) == (1, True, s.fills, s.opened)
    assert (r.exiting, r.working, r.held) == (s.exiting, None, None) and r.exiting is not None
    assert r.unreconciled and reason(core.on_bar(r, at("09:36"))[0]).startswith("paused")
    # restarted while the second entry was still working: it comes back as working
    r = restart(upto, pending)
    assert (r.working, r.position) == (pending.working, core.FLAT)
    assert r.acct.balance == pytest.approx(pending.acct.balance)


def test_a_drift_pause_survives_restart_and_so_does_its_resolve():
    b = at("09:30")
    s, journal = decide_now(ready(sim(), b), [LONG])
    s, effects = core.on_fill(
        s, fill(1, 20002.0, b.ts + timedelta(seconds=61), cid=s.working.client_id)
    )
    journal += effects

    def restart():
        rows = [(e.kind, e.payload) for e in journal if isinstance(e, Log)]
        return core.replay(core.start(sim(), at("09:32").ts, halts(journal))[0], rows)

    assert restart().drift == s.drift is not None
    assert core.account_status(restart())["halt_kind"] == "drift"
    oco = [order("stop", "sell", 1, 19990.0), order("target", "sell", 1, 20020.0, oid="o2")]
    s, effects = core.resolve(s, INSTRUMENT, [held(1, 20002.0)], oco, at("09:32").ts)
    journal += effects
    assert s.drift is None and restart().drift is None  # replaying the fill does not raise it


def restarted_after(setup, nt_balance, rows, now):
    """`start` with NinjaTrader's balance at the restart (absolute $), then `replay`."""
    begin = replace(setup.start, balance=nt_balance)
    s = core.start(replace(setup, start=begin), now, [p for k, p in rows if k == "halt"])[0]
    return core.replay(s, rows)


def made_200():
    """A sim day that made +$200 (entry 20000, exit 20010), and its journal rows."""
    b, rows = at("09:30"), []
    s, effects = decide_now(ready(sim(), b), [LONG])
    rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    for f in (
        fill(1, 20000.0, b.ts + timedelta(seconds=61), cid=s.working.client_id),
        fill(-1, 20010.0, at("09:32").ts),
    ):
        s, effects = core.on_fill(s, f)
        rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    return s, rows


def test_replay_without_day_start_keeps_ninjatraders_balance():
    s, rows = made_200()
    assert s.acct.balance == pytest.approx(200.0)
    setup = replace(sim(), day_start=None)  # its balance now already holds the day's fills
    r = restarted_after(setup, 50_200.0, rows, at("09:40").ts)
    assert r.acct.balance == pytest.approx(200.0)  # not 400: booked once
    assert (r.position, r.fills, r.opened) == (s.position, s.fills, s.opened)
    assert r.acct.traded_today is False  # as start had it


def test_replay_never_loosens_ninjatraders_balance():
    b, rows = at("09:30"), []
    s, effects = decide_now(ready(sim(), b), [LONG])
    rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    s, effects = core.on_fill(
        s, fill(1, 20000.0, b.ts + timedelta(seconds=61), cid=s.working.client_id)
    )
    rows += [(e.kind, e.payload) for e in effects if isinstance(e, Log)]
    # down 10:00-10:05: the stop filled at 19989 (-$220) and was never journalled
    r = restarted_after(sim(), 50_000.0 - 220.0, rows, at("10:05").ts)
    assert r.acct.balance == pytest.approx(-220.0) and r.acct.traded_today
    r, _ = core.on_reconcile(r, INSTRUMENT, (), (), at("10:05").ts)
    assert r.acct.balance == pytest.approx(-220.0)
    # a whole trade missed: still a traded day
    r = restarted_after(sim(), 50_000.0 - 220.0, [], at("10:05").ts)
    assert r.acct.balance == pytest.approx(-220.0) and r.acct.traded_today
    # a missed profit stays unbooked; the rules' commission keeps the app's own stricter
    _, rows = made_200()
    r = restarted_after(sim(), 50_300.0, rows, at("09:40").ts)
    assert r.acct.balance == pytest.approx(200.0)


def test_a_mismatched_reconcile_still_ends_the_outage():
    s = ready(sim(), at("09:30"))
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    stray = order("other", "buy", 1, 19900.0, cid="", oid="manual")
    s, _ = core.on_reconcile(s, INSTRUMENT, (), [stray], at("09:31").ts)
    assert s.unreconciled is None and s.mismatch
    s, effects = core.on_disconnect(s, "no heartbeat for 6 s")  # the next outage alerts
    assert kinds(effects) == ["alert"]


def test_a_flat_by_breach_after_a_mismatched_reconcile_is_a_breach():
    s = entered(sim(), at("16:30"))
    oco = [order("stop", "sell", 1, 19990.0), order("target", "sell", 1, 20020.0, oid="o2")]
    stray = replace(order("other", "buy", 1, 4000.0, oid="es"), instrument="ES 12-25")
    s, _ = core.on_disconnect(s, "no heartbeat for 6 s")
    s, _ = core.on_reconcile(s, INSTRUMENT, [held(1)], [*oco, stray], at("16:31").ts)
    assert s.mismatch and s.unreconciled is None  # NinjaTrader just reported the long
    s, _ = core.on_bar(s, at("16:45"))
    assert s.halt_kind == "breach" and "flat_by" in s.halted


@pytest.mark.parametrize(
    "stop",
    [
        order("stop", "buy", 1, 19990.0),  # adds to the long: no protection
        replace(order("stop", "sell", 1, 19990.0), account="Sim102"),  # another account's
        replace(order("stop", "sell", 1, 19990.0), qty=0),  # nothing left
    ],
)
def test_only_a_closing_stop_on_this_account_protects(stop):
    s = ready(sim(), at("09:30"))
    target = order("target", "sell", 1, 20020.0, oid="o2")
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(1)], [stop, target], at("09:31").ts)
    assert s.position.stop is None and [e.action for e in sends(effects)] == [Flatten()]


def test_a_negative_qty_stop_never_counts_toward_cover():
    s = ready(sim(), at("09:30"))
    stops = [order("stop", "sell", 2, 19990.0), order("stop", "sell", -1, 19995.0, oid="o3")]
    s, effects = core.on_reconcile(s, INSTRUMENT, [held(1)], stops, at("09:31").ts)
    assert s.position.stop is None and [e.action for e in sends(effects)] == [Flatten()]


def test_only_an_entry_order_keeps_the_working_entry():
    s, _ = decide_now(ready(sim(), at("09:30")), [LONG])
    stop = order("stop", "sell", 1, 19990.0, cid=s.working.client_id)  # not the entry
    s, _ = core.on_reconcile(s, INSTRUMENT, (), [stop], at("09:31").ts)
    assert s.working is None and "stray_orders" in s.mismatch


def test_each_reconcile_flatten_has_its_own_id():
    s = ready(sim(), at("09:30"))
    t = at("09:31").ts
    ids = [
        sends(core.on_reconcile(s, INSTRUMENT, [held(1)], (), t + timedelta(seconds=sec))[1])[0]
        for sec in (5, 5, 20)
    ]
    assert ids[0] == ids[1] and ids[0].client_id != ids[2].client_id  # a flap in one minute
