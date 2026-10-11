"""Pure core of the browser terminal: child environment, shell choice, client messages.

Client → server text frames: `{"type":"input","data":"..."}` or
`{"type":"resize","cols":N,"rows":M}`. Server → client: binary frames of raw PTY output.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

MAX_SIZE = 1000


@dataclass(frozen=True, slots=True)
class Input:
    data: str


@dataclass(frozen=True, slots=True)
class Resize:
    cols: int
    rows: int


def child_env(env: Mapping[str, str], secret: str) -> dict[str, str]:
    """The shell's environment: no FENRIR_* var and nothing whose value holds `secret`."""
    kept = {
        k: v for k, v in env.items() if not k.startswith("FENRIR_") and not (secret and secret in v)
    }
    return kept | {"TERM": "xterm-256color"}


def shell_argv(env: Mapping[str, str], platform: str) -> list[str]:
    """`platform` is `sys.platform`."""
    if platform == "win32":
        return ["powershell.exe"]
    return [env.get("SHELL") or "/bin/bash"]


def _size(n: object) -> int | None:
    # bool is an int subclass; JSON true is not a size
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    return max(1, min(n, MAX_SIZE))


def parse_message(text: str) -> Input | Resize | None:
    """A client frame, or None for anything malformed."""
    try:
        msg = json.loads(text)
    except (ValueError, RecursionError):  # ValueError also covers ints past the digit limit
        return None
    if not isinstance(msg, dict):
        return None
    match msg.get("type"):
        case "input" if isinstance(msg.get("data"), str):
            return Input(msg["data"])
        case "resize":
            cols, rows = _size(msg.get("cols")), _size(msg.get("rows"))
            if cols is not None and rows is not None:
                return Resize(cols, rows)
    return None
