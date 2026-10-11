"""Child process that runs one backtest job's items (DEC-1: strategy code never runs in the server).

stdin: `{"include_holdout": bool, "items": [[index, strategy, account, contract], ...]}`.
stdout: one JSON event per line, parsed by `jobs_runner.parse_event`. Settings come from the
environment only (the runner passes the dirs, never secrets). Shell.
"""

import json
import os
import sys
from pathlib import Path

from fenrir.backtest.cache import cached_run
from fenrir.backtest.grid import GridItem
from fenrir.settings import parse_settings


def main() -> None:
    out = os.fdopen(os.dup(1), "w", buffering=1)  # the dup is not inherited by spawned workers
    os.dup2(2, 1)  # strategy prints, here or in a worker, go to stderr, not into the protocol

    def emit(**event) -> None:
        out.write(json.dumps(event) + "\n")

    job = json.load(sys.stdin)
    settings = parse_settings(os.environ)
    for i, strategy, account, contract in job["items"]:
        emit(i=i, status="running")
        try:
            report = cached_run(
                GridItem(Path(strategy), account, contract),
                settings,
                include_holdout=job["include_holdout"],
                on_progress=lambda done, total, i=i: emit(i=i, progress=[done, total]),
            )
            emit(i=i, status="done", key=report["key"])
        except Exception as e:  # one bad pair must not stop the rest of the grid
            emit(i=i, status="error", error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":  # spawned pool workers import this module as __mp_main__
    main()
