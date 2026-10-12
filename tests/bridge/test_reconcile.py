import pytest

from fenrir.bridge.reconcile import ExpectedBook, Mismatch, NtOrder, NtPosition, reconcile

NQ = "NQ 12-26"
ES = "ES 12-26"


def pos(qty, instrument=NQ, account="Sim101"):
    return NtPosition(account=account, instrument=instrument, qty=qty, avg_price=20000.0)


def order(role, qty=1, action="sell", instrument=NQ, account="Sim101", order_id="o1"):
    return NtOrder(
        account=account,
        order_id=order_id,
        client_id="c1",
        instrument=instrument,
        role=role,
        action=action,
        qty=qty,
        price=19990.0,
        state="Working",
    )


def kinds(mismatches):
    return [m.kind for m in mismatches]


def book(qty):
    return ExpectedBook("Sim101", NQ, qty)


LONG_1 = [order("stop", order_id="s"), order("target", order_id="t")]


@pytest.mark.parametrize(
    ("expected", "positions", "orders", "want"),
    [
        # clean books
        pytest.param(0, [], [], [], id="flat-no-orders"),
        pytest.param(1, [pos(1)], LONG_1, [], id="long-protected"),
        pytest.param(
            -2,
            [pos(-2)],
            [order("stop", 2, "buy", order_id="s"), order("target", 2, "buy", order_id="t")],
            [],
            id="short-protected",
        ),
        pytest.param(
            3,
            [pos(3)],
            [
                order("stop", 2, order_id="s1"),
                order("stop", 1, order_id="s2"),
                order("target", 3, order_id="t"),
            ],
            [],
            id="stop-split-across-orders",
        ),
        pytest.param(0, [pos(0)], [], [], id="zero-qty-row-is-flat"),
        pytest.param(
            0,
            [pos(1, account="Sim102")],
            [order("stop", account="Sim102")],
            [],
            id="other-accounts-ignored",
        ),
        # position mismatches
        pytest.param(
            0,
            [pos(2)],
            [order("stop", 2, order_id="s"), order("target", 2, order_id="t")],
            ["position"],
            id="quantity-differs",
        ),
        pytest.param(1, [], [], ["position"], id="runner-long-nt-flat"),
        pytest.param(
            0, [pos(1, instrument=ES)], [], ["unexpected_position"], id="other-instrument"
        ),
        # protection
        pytest.param(1, [pos(1)], [order("target")], ["missing_stop"], id="no-stop"),
        pytest.param(1, [pos(1)], [order("stop")], ["missing_target"], id="no-target"),
        pytest.param(
            -3,
            [pos(-3)],
            [order("stop", 1, "buy", order_id="s"), order("target", 3, "buy", order_id="t")],
            ["missing_stop"],
            id="stop-covers-too-few",
        ),
        pytest.param(
            1,
            [pos(1)],
            [order("stop", 2, order_id="s"), order("target", order_id="t")],
            ["missing_stop"],
            id="stop-covers-too-many",
        ),
        pytest.param(
            1,
            [pos(1)],
            [order("stop", action="buy", order_id="s"), order("target", order_id="t")],
            ["missing_stop", "stray_orders"],
            id="wrong-direction-stop",
        ),
        pytest.param(
            1,
            [pos(1)],
            [order("stop", instrument=ES, order_id="s"), order("target", order_id="t")],
            ["missing_stop", "stray_orders"],
            id="stop-on-other-instrument",
        ),
        # stray orders
        pytest.param(0, [], [order("stop")], ["stray_orders"], id="flat-with-working-order"),
        pytest.param(
            1,
            [pos(1)],
            [*LONG_1, order("entry", action="buy", order_id="e")],
            ["stray_orders"],
            id="stale-entry-while-held",
        ),
        pytest.param(
            1,
            [pos(1)],
            [*LONG_1, order("other", order_id="x")],
            ["stray_orders"],
            id="other-order-while-held",
        ),
    ],
)
def test_reconcile(expected, positions, orders, want):
    assert kinds(reconcile(book(expected), positions, orders)) == want


def test_stray_detail_names_the_orders():
    result = reconcile(book(1), [pos(1)], [*LONG_1, order("entry", action="buy", order_id="e9")])
    assert result == (Mismatch("stray_orders", "working orders while holding 1 NQ 12-26: e9"),)


def test_all_kinds_together_in_fixed_order():
    result = reconcile(book(2), [pos(1), pos(-1, instrument=ES)], [order("entry", order_id="e")])
    assert kinds(result) == [
        "unexpected_position",
        "position",
        "missing_stop",
        "missing_target",
        "stray_orders",
    ]
