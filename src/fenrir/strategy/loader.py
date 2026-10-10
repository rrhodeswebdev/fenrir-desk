"""Load a strategy file: scan imports, hash the bytes, execute exactly those bytes. Shell."""

import ast
import hashlib
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fenrir.core.actions import Action
from fenrir.core.bars import Bar
from fenrir.strategy.types import Ctx

MARKETS = ("ES", "NQ")
# Modules that give a strategy I/O, a clock or randomness. A static check, not a
# sandbox.
FORBIDDEN_IMPORTS = frozenset(
    {
        "asyncio",
        "io",
        "os",
        "pathlib",
        "random",
        "requests",
        "httpx",
        "secrets",
        "shutil",
        "socket",
        "subprocess",
        "sys",
        "threading",
        "time",
        "urllib",
        "uuid",
    }
)


@dataclass(frozen=True, slots=True)
class Strategy:
    name: str  # file stem
    path: Path
    hash: str  # sha256 hex of file bytes
    market: str  # "ES" | "NQ"
    params: Mapping[str, Any]
    init: Callable[[Mapping[str, Any]], Any]
    step: Callable[[Any, Bar, Ctx], tuple[Any, list[Action]]]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _imported_roots(tree: ast.Module) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def load_strategy(path: Path) -> Strategy:
    source = path.read_bytes()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise ValueError(f"{path}: syntax error: {e}") from e
    forbidden = sorted(_imported_roots(tree) & FORBIDDEN_IMPORTS)
    if forbidden:
        raise ValueError(f"{path}: forbidden import {', '.join(forbidden)} (step must be pure)")

    digest = hashlib.sha256(source).hexdigest()
    name = f"fenrir_strategy_{path.stem}_{digest[:12]}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module  # dataclasses defined in the file resolve their module
    # here
    exec(compile(tree, str(path), "exec"), module.__dict__)

    missing = [n for n in ("MARKET", "PARAMS", "init", "step") if not hasattr(module, n)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    if module.MARKET not in MARKETS:
        raise ValueError(f"{path}: MARKET must be one of ES, NQ, got {module.MARKET!r}")
    if not isinstance(module.PARAMS, dict):
        raise ValueError(f"{path}: PARAMS must be a dict")
    if not (callable(module.init) and callable(module.step)):
        raise ValueError(f"{path}: init and step must be functions")
    return Strategy(
        name=path.stem,
        path=path,
        hash=digest,
        market=module.MARKET,
        params=dict(module.PARAMS),
        init=module.init,
        step=module.step,
    )
