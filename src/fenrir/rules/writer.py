"""Save edited rules into their YAML file, changing only the values that differ (DEC-3)."""

import io
import os
import secrets
import stat
from functools import reduce
from operator import getitem
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.emitter import Emitter

from fenrir.files import read_regular
from fenrir.rules.loader import RULES_MAX, parse_account
from fenrir.rules.schema import AccountRules

Loc = tuple[str | int, ...]


def changes(old: Any, new: Any, at: Loc = ()) -> list[tuple[Loc, Any]]:
    """(path, new value) for each value in `new` that differs from `old`; 2100 == 2100.0."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [c for k in new for c in changes(old.get(k), new[k], (*at, k))]
    seqs = (list, tuple)
    if isinstance(old, seqs) and isinstance(new, seqs) and len(old) == len(new):
        return [
            c
            for i, (o, n) in enumerate(zip(old, new, strict=True))
            for c in changes(o, n, (*at, i))
        ]
    return [] if old == new else [(at, new)]


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        node = CommentedMap((k, _plain(v)) for k, v in value.items())
        if not any(isinstance(v, dict | list | tuple) for v in value.values()):
            node.fa.set_flow_style()  # a map of plain values is one `{ a: 1 }` line in the files
        return node
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)  # the files write 2100, not 2100.0
    return value


class _Emitter(Emitter):
    """Writes flow maps as `{ a: 1 }`, the style the rules files use."""

    def write_indicator(self, indicator, need_whitespace, whitespace=False, indention=False):
        if indicator.endswith("{"):
            indicator += " "
        elif indicator == "}":
            indicator = " }"
        super().write_indicator(indicator, need_whitespace, whitespace, indention)


def _yaml() -> YAML:
    y = YAML()  # round-trip: keeps comments, key order and flow style
    y.preserve_quotes = True  # else "16:45" is written bare, and PyYAML reads that as an int
    y.width = 4096  # never re-wrap a line
    y.indent(mapping=2, sequence=4, offset=2)
    y.Emitter = _Emitter
    y.representer.add_representer(
        type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def _set(doc: Any, at: Loc, value: Any) -> None:
    *parents, key = at
    node = reduce(getitem, parents, doc)
    old = node.get(key) if isinstance(node, dict) else node[key]
    # keep a quoted string quoted
    node[key] = type(old)(value) if isinstance(old, str) and isinstance(value, str) else value


def write_account(path: Path, rules: AccountRules) -> None:
    """Save `rules` over the account file at `path`; raises ValueError and writes nothing
    if the file does not load now, or would not load as `rules` afterwards."""
    aid = f"{path.parent.name}-{path.stem}"
    text = read_regular(path, RULES_MAX).decode("utf-8")
    edits = changes(parse_account(text, aid, path).model_dump(), rules.model_dump())
    if not edits:
        return
    y = _yaml()
    doc = y.load(text)
    for at, value in edits:
        _set(doc, at, _plain(value))
    out = io.StringIO()
    y.dump(doc, out)
    try:
        saved = parse_account(out.getvalue(), aid, path)
    except ValueError as e:
        raise ValueError(f"the value can't be saved as YAML: {e}") from e
    if changes(saved.model_dump(), rules.model_dump()):
        raise ValueError(f"the value can't be saved as YAML: {path} would not read back the same")
    st = path.lstat()  # never a link's target: its mode and owner would land on the new file
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{path} is not a regular file; not saving over it")
    # rules/ is terminal-writable and the server saves as root (DEC-1, #166): a random name
    # opened O_EXCL|O_NOFOLLOW, its mode and owner set on the fd, so a planted link is never
    # followed. Windows has no O_NOFOLLOW (and no terminal user to plant a link).
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(out.getvalue())
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), stat.S_IMODE(st.st_mode))
            # a save from Docker must not leave the host a root-owned file; Windows has no geteuid
            if getattr(os, "geteuid", lambda: -1)() == 0:
                os.fchown(f.fileno(), st.st_uid, st.st_gid)
        os.replace(tmp, path)  # atomic: readers see the old file or the new one, never half
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
