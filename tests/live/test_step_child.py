import json
from dataclasses import replace

import pytest
from backtest_helpers import NQ, bar

from fenrir.core.actions import Bracket, Cancel, Flatten
from fenrir.live.service import parse_reply
from fenrir.live.step_child import ctx_json, parse_ctx
from fenrir.rules.state import AccountView
from fenrir.strategy import FLAT, Ctx


def test_ctx_survives_the_pipe():
    bars = (bar("09:30", 1.0, 2.0, 0.5, 1.5), bar("09:31", 1.5, 2.5, 1.0, 2.25))
    c = Ctx(
        bars[-1].ts,
        bars,
        replace(FLAT, qty=-1, entry=1.5),
        AccountView("evaluation", 2000.0, None, -12.5, 3000.0),
        NQ,
    )
    assert parse_ctx(json.loads(json.dumps(ctx_json(c)))) == c


def test_parses_each_action():
    line = json.dumps(
        {
            "actions": [
                {"type": "Bracket", "side": "long", "qty": 1, "stop": 9.5, "target": 12},
                {"type": "Flatten"},
                {"type": "Cancel", "order_id": "a1"},
            ]
        }
    )
    assert parse_reply(line.encode()) == {
        "actions": (Bracket("long", 1, 9.5, 12), Flatten(), Cancel("a1"))
    }


def test_load_reply_passes_through():
    assert parse_reply(b'{"hash": "ab"}\n') == {"hash": "ab"}


def _bracket(qty: bytes) -> bytes:
    fields = b'"side": "long", "qty": ' + qty + b', "stop": 1, "target": 2'
    return b'{"actions": [{"type": "Bracket", ' + fields + b"}]}"


@pytest.mark.parametrize(
    "line, message",
    [
        (b"", "the strategy process exited"),
        (b'{"error": "ZeroDivisionError: division by zero"}\n', "ZeroDivisionError"),
        (b"not json\n", "not a strategy reply"),
        (b"[1]\n", "not a strategy reply"),
        (_bracket(b"NaN"), "NaN"),
        (_bracket(b"1e999"), "not an action"),  # parses as inf
        (_bracket(b"1" + b"0" * 400), "not an action"),  # float() overflows
        (b'{"actions": [{"type": "Order"}]}', "not an action"),
        (b'{"actions": [{"type": "Flatten", "now": true}]}', "not an action"),
        (_bracket(b'"1"'), "not an action"),
        (b'{"actions": {}}', "not a strategy reply"),
    ],
)
def test_untrusted_replies_are_refused(line, message):
    with pytest.raises(ValueError, match=message):
        parse_reply(line)
