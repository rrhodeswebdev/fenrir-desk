"""Expand strategy globs x account globs x contracts into grid items. Shell: reads the disk."""

import glob
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from fenrir.core.instruments import load_instruments, spec
from fenrir.rules.loader import list_accounts
from fenrir.settings import Settings
from fenrir.strategy.loader import load_strategy


@dataclass(frozen=True, slots=True)
class GridItem:
    strategy_path: Path
    account_id: str
    contract: str


def _expand[T](patterns: Sequence[str], match: Callable[[str], Iterable[T]]) -> list[T]:
    """Every match of every pattern, sorted, each once; a pattern matching nothing raises."""
    found: set[T] = set()
    for pattern in patterns:
        hits = set(match(pattern))
        if not hits:
            raise ValueError(f"{pattern!r} matches nothing")
        found |= hits
    return sorted(found)


def expand_grid(
    strategy_globs: Sequence[str],
    account_globs: Sequence[str],
    contracts: Sequence[str] | None,
    settings: Settings,
) -> tuple[GridItem, ...]:
    """`contracts` None = both symbols of each strategy's MARKET; otherwise those that fit it."""
    paths = _expand(strategy_globs, lambda p: map(Path, glob.glob(p)))
    known = list_accounts(settings.rules_dir)
    accounts = _expand(account_globs, lambda p: (a for a in known if fnmatchcase(a, p)))
    instruments = load_instruments()
    specs = (
        instruments.specs
        if contracts is None
        else [spec(instruments, c) for c in dict.fromkeys(contracts)]
    )
    markets = {path: load_strategy(path).market for path in paths}
    items = tuple(
        GridItem(path, account, s.symbol)
        for path in paths
        for account in accounts
        for s in specs
        if s.series == markets[path]
    )
    if not items:
        raise ValueError(f"empty grid: no strategy trades any of {', '.join(contracts or ())}")
    return items
