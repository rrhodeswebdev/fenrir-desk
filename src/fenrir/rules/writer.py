"""Save edited rules into their YAML file, changing only the values that differ (DEC-3)."""

import io
import os
from functools import reduce
from operator import getitem
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.emitter import Emitter

from fenrir.rules.loader import parse_account
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
    text = path.read_text(encoding="utf-8")
    edits = changes(parse_account(text, aid, path).model_dump(), rules.model_dump())
    if not edits:
        return
    y = _yaml()
    doc = y.load(text)
    for at, value in edits:
        _set(doc, at, _plain(value))
    out = io.StringIO()
    y.dump(doc, out)
    if changes(parse_account(out.getvalue(), aid, path).model_dump(), rules.model_dump()):
        raise ValueError(f"{path}: the saved file would not read back as these rules")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(out.getvalue(), encoding="utf-8")
    os.replace(tmp, path)  # atomic: readers see the old file or the new one, never half
