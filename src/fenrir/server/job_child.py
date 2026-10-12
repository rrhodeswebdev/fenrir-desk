"""Child process that runs strategy code for the job runner (DEC-1: never in the server).

`python -P -m fenrir.server.job_child`: stdin `{"include_holdout": bool, "items": [[index,
strategy, account, contract], ...]}`; stdout one JSON event per line (`jobs_runner.parse_event`).
`... job_child --expand`: stdin `{"strategies", "accounts", "contracts"}` (`expand_grid` loads
each strategy to read its MARKET); stdout one line, `[[strategy, account, contract], ...]` or
`{"error": "..."}` (`jobs_runner.parse_grid`).
`... job_child --describe`: stdin `{"paths": [...]}`; stdout one line per path loaded, `{path,
name, hash, market, params, error}` (`jobs_runner.parse_description`). Settings come from the
environment only (the runner passes the dirs, never secrets). Shell.
"""

import json
import os
import sys
from pathlib import Path

from fenrir.backtest.cache import cached_run
from fenrir.backtest.grid import GridItem, expand_grid
from fenrir.settings import parse_settings
from fenrir.strategy.loader import file_hash, load_strategy


def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _describe(path: Path) -> str:
    entry = {"path": str(path), "name": path.stem, "hash": None, "market": None, "params": {}}
    try:
        entry["hash"] = file_hash(path)
        s = load_strategy(path)
        # the hash of the bytes that ran; allow_nan=False: NaN is not JSON
        loaded = {"hash": s.hash, "market": s.market, "params": dict(s.params), "error": None}
        return json.dumps(entry | loaded, allow_nan=False)
    except BaseException as e:  # a file that raises SystemExit must not hide the rest
        return json.dumps(entry | {"error": _error(e)})


def main(args: list[str]) -> None:
    out = os.fdopen(os.dup(1), "w", buffering=1)  # the dup is not inherited by spawned workers
    os.dup2(2, 1)  # strategy prints, here or in a worker, go to stderr, not into the protocol

    def emit(event) -> None:
        out.write(json.dumps(event) + "\n")

    request = json.load(sys.stdin)
    settings = parse_settings(os.environ)
    if args == ["--describe"]:
        for path in request["paths"]:
            out.write(_describe(Path(path)) + "\n")
        return
    if args == ["--expand"]:
        try:
            grid = expand_grid(
                request["strategies"], request["accounts"], request["contracts"], settings
            )
            emit([[str(g.strategy_path), g.account_id, g.contract] for g in grid])
        except Exception as e:
            emit({"error": _error(e)})
        return
    for i, strategy, account, contract in request["items"]:
        emit({"i": i, "status": "running"})
        try:
            report = cached_run(
                GridItem(Path(strategy), account, contract),
                settings,
                include_holdout=request["include_holdout"],
                on_progress=lambda done, total, i=i: emit({"i": i, "progress": [done, total]}),
            )
            emit({"i": i, "status": "done", "key": report["key"]})
        except Exception as e:  # one bad pair must not stop the rest of the grid
            emit({"i": i, "status": "error", "error": _error(e)})


if __name__ == "__main__":  # spawned pool workers import this module as __mp_main__
    main(sys.argv[1:])
