"""Child process that runs one armed strategy for the live hub (DEC-1: never in the server).

`python -P -m fenrir.live.step_child PATH`: loads PATH and calls `init(PARAMS)`, then writes one
line, `{"hash": sha256 of the bytes loaded}` or `{"error": "..."}` (and exits). Then, per stdin
line `{"ctx": ctx_json(ctx)}`, it calls `step(state, ctx.bars[-1], ctx)` and writes
`{"actions": [{type, **fields}, ...]}` or `{"error": "..."}` (and exits). The strategy's state
lives here, so one child serves one account for as long as it runs. `ctx_json`/`parse_ctx` are
pure; `main` is shell.
"""

import json
import os
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fenrir.core.bars import Bar
from fenrir.core.instruments import ContractSpec
from fenrir.rules.state import AccountView
from fenrir.strategy import Ctx, Position, load_strategy


def ctx_json(c: Ctx) -> dict[str, Any]:
    """`c` as JSON; `c.ts` is the last bar's."""
    return {
        "bars": [asdict(b) | {"ts": b.ts.isoformat()} for b in c.bars],
        "position": asdict(c.position),
        "account": asdict(c.account),
        "spec": asdict(c.spec),
    }


def ctx_lines(ctxs: Iterable[Ctx]) -> Iterator[bytes]:
    """The `{"ctx": ctx_json(c)}` request line for each of `ctxs`, each bar encoded once: a
    warm-up (#79) replays a day of contexts whose windows share up to 500 bars."""
    encoded: dict[Bar, str] = {}
    for c in ctxs:
        bars = ",".join(
            encoded.get(b)
            or encoded.setdefault(b, json.dumps(asdict(b) | {"ts": b.ts.isoformat()}))
            for b in c.bars
        )
        rest = json.dumps(
            {"position": asdict(c.position), "account": asdict(c.account), "spec": asdict(c.spec)}
        )
        yield f'{{"ctx": {{"bars": [{bars}], {rest[1:]}}}\n'.encode()


def parse_ctx(d: dict[str, Any]) -> Ctx:
    bars = tuple(Bar(**b | {"ts": datetime.fromisoformat(b["ts"])}) for b in d["bars"])
    return Ctx(
        bars[-1].ts,
        bars,
        Position(**d["position"]),
        AccountView(**d["account"]),
        ContractSpec(**d["spec"]),
    )


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def main(args: list[str]) -> None:
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # strategy prints go to stderr, not into the protocol

    def emit(reply: dict[str, Any]) -> None:
        out.write(json.dumps(reply, allow_nan=False) + "\n")

    try:
        strategy = load_strategy(Path(args[0]))
        state = strategy.init(strategy.params)
    except BaseException as e:  # SystemExit too: the hub must hear why
        return emit({"error": _error(e)})
    emit({"hash": strategy.hash})
    for line in sys.stdin:
        c = parse_ctx(json.loads(line)["ctx"])
        try:
            state, actions = strategy.step(state, c.bars[-1], c)
            emit({"actions": [{"type": type(a).__name__, **asdict(a)} for a in actions]})
        except BaseException as e:  # a non-dataclass action or NaN fails here too
            return emit({"error": _error(e)})


if __name__ == "__main__":
    main(sys.argv[1:])
