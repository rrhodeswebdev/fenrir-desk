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

job = json.load(sys.stdin)
for i, strategy, _account, _contract in job["items"]:

    def emit(i=i, **event):
        print(json.dumps({"i": i, **event}), flush=True)

    path = Path(strategy)
    emit(status="running")
    if path.stem == "crash":
        sys.exit(3)
    if path.stem == "hang":
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        info = {"sleeper": sleeper.pid, "own_session": os.getsid(0) == os.getpid()}
        path.with_suffix(".tmp").write_text(json.dumps(info))
        path.with_suffix(".tmp").rename(path.with_suffix(".json"))  # whole, never half-written
        time.sleep(60)
    if path.stem == "env":
        names = sorted(k for k in os.environ if k.startswith("FENRIR_"))
        emit(status="error", error=" ".join(names))
        continue
    print("not a protocol line", flush=True)
    for done in range(1, 201):
        emit(progress=[done, 200])
    if path.stem == "bad":
        emit(status="error", error="ValueError: boom")
    else:
        emit(status="done", key=hashlib.sha256(path.stem.encode()).hexdigest())
