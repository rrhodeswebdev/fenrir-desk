import hashlib
import sys
from pathlib import Path

import pytest

from fenrir.strategy import file_hash, load_strategy

GOOD = """
MARKET = "NQ"
PARAMS = {"n": 3}

def init(params):
    return params["n"]

def step(state, bar, ctx):
    return state, []
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_loads_valid_strategy(tmp_path):
    path = write(tmp_path / "s.py", GOOD)
    s = load_strategy(path)
    assert s.name == "s"
    assert s.path == path
    assert s.market == "NQ"
    assert s.params == {"n": 3}
    assert s.init(s.params) == 3
    assert s.step(3, None, None) == (3, [])
    assert s.hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_hash(path) == s.hash


@pytest.mark.parametrize("missing", ["MARKET", "PARAMS", "init", "step"])
def test_rejects_missing_export(tmp_path, missing):
    text = GOOD.replace(f"{missing} =", f"_{missing} =").replace(
        f"def {missing}(", f"def _{missing}("
    )
    path = write(tmp_path / "s.py", text)
    with pytest.raises(ValueError, match=f"missing {missing}"):
        load_strategy(path)


def test_rejects_unknown_market(tmp_path):
    path = write(tmp_path / "s.py", GOOD.replace('"NQ"', '"CL"'))
    with pytest.raises(ValueError, match="MARKET must be one of ES, NQ"):
        load_strategy(path)


def test_rejects_non_dict_params(tmp_path):
    path = write(tmp_path / "s.py", GOOD.replace('{"n": 3}', "[3]"))
    with pytest.raises(ValueError, match="PARAMS must be a dict"):
        load_strategy(path)


@pytest.mark.parametrize(
    "line",
    [
        "import random",
        "import os",
        "from os import path",
        "import urllib.request",
        "import time as t",
    ],
)
def test_rejects_forbidden_imports(tmp_path, line):
    path = write(tmp_path / "s.py", line + "\n" + GOOD)
    with pytest.raises(ValueError, match="forbidden import"):
        load_strategy(path)


def test_syntax_error_names_the_file(tmp_path):
    path = write(tmp_path / "broken.py", "def step(:\n")
    with pytest.raises(ValueError, match="broken.py"):
        load_strategy(path)


def test_same_stem_in_two_dirs_keeps_both(tmp_path):
    a = load_strategy(write(tmp_path / "a" / "orb.py", GOOD))
    b = load_strategy(write(tmp_path / "b" / "orb.py", GOOD.replace('{"n": 3}', '{"n": 7}')))
    assert a.init(a.params) == 3
    assert b.init(b.params) == 7


def test_edited_file_reloads_new_code_and_hash(tmp_path):
    path = write(tmp_path / "s.py", GOOD)
    first = load_strategy(path)
    write(path, GOOD.replace('return params["n"]', 'return params["n"] * 10'))
    second = load_strategy(path)
    assert second.hash != first.hash
    assert second.init(second.params) == 30
    assert first.init(first.params) == 3


def test_dataclass_state_works(tmp_path):
    base = (
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class S:\n    n: int\n"
    )
    text = base + GOOD.replace('return params["n"]', 'return S(params["n"])')
    s = load_strategy(write(tmp_path / "d.py", text))
    assert s.init(s.params).n == 3


@pytest.mark.parametrize(
    "body", ["import numpy_not_installed\n", "x = undefined_name\n"], ids=["import", "name"]
)
def test_exec_errors_become_value_error_and_unregister(tmp_path, body):
    path = write(tmp_path / "execfail_zq.py", body + GOOD)
    with pytest.raises(ValueError, match="execfail_zq.py"):
        load_strategy(path)
    assert not [k for k in sys.modules if k.startswith("fenrir_strategy_execfail_zq_")]


@pytest.mark.parametrize(
    "call, header",
    [
        ("datetime.now()", "from datetime import datetime\n"),
        ("datetime.datetime.utcnow()", "import datetime\n"),
        ("date.today()", "from datetime import date\n"),
    ],
)
def test_rejects_clock_calls(tmp_path, call, header):
    text = header + GOOD.replace("return state, []", f"{call}\n    return state, []")
    with pytest.raises(ValueError, match="clock call"):
        load_strategy(write(tmp_path / "clock.py", text))
