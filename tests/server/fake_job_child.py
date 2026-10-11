"""Stands in for `fenrir.server.job_child`: same stdin/stdout protocol, no backtest.

What it does per item depends on the strategy file's stem (see test_jobs_runner.py).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)


def hang(path: Path) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    info = {"sleeper": sleeper.pid, "own_session": os.getsid(0) == os.getpid()}
    path.with_suffix(".tmp").write_text(json.dumps(info))
    path.with_suffix(".tmp").rename(path.with_suffix(".json"))  # whole, never half-written
    time.sleep(60)


if sys.argv[1:] == ["--expand"]:
    answer, *rest = request["strategies"]
    if answer == "hang":
        hang(Path(rest[0]))
    print(answer, flush=True)  # the test passes the raw answer line as the first "glob"
    sys.exit()

for i, strategy, _account, _contract in request["items"]:

    def emit(i=i, **event):
        print(json.dumps({"i": i, **event}), flush=True)

    path = Path(strategy)
    emit(status="running")
    if path.stem == "crash":
        sys.exit(3)
    if path.stem == "hang":
        hang(path)
    if path.stem == "env":
        names = sorted(k for k, v in os.environ.items() if k.startswith("FENRIR_") or "sekrit" in v)
        emit(status="error", error=" ".join(names))
        continue
    if path.stem == "flood":
        emit(status="error", error="y" * 5000)
        # overlong: dropped whole, so the valid-looking tail past LINE_MAX is never parsed
        print(" " * 65536 + json.dumps({"i": i, "status": "error", "error": "smuggled"}))
        sys.stdout.write(json.dumps({"i": i, "status": "error", "error": "no newline"}))
        sys.exit()
    print("not a protocol line", flush=True)
    for done in range(1, 201):
        emit(progress=[done, 200])
    if path.stem == "bad":
        emit(status="error", error="ValueError: boom")
    else:
        emit(status="done", key=hashlib.sha256(path.stem.encode()).hexdigest())
