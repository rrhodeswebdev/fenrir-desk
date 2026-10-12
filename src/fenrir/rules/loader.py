"""Account id <-> rules file. Reads rules files; `writer.py` writes them."""

import re
from pathlib import Path

import yaml

from fenrir.files import read_regular, refuse_symlink
from fenrir.rules.schema import AccountRules

RULES_MAX = 1 << 20

_ID = re.compile(r"([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)")


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a repeated key; PyYAML keeps the last one silently."""

    def construct_mapping(self, node, deep=False):
        keys = [
            self.construct_object(k, deep=deep)
            for k, _ in node.value
            if k.tag != "tag:yaml.org,2002:merge"  # `<<: *anchor` may be overridden
        ]
        for key in keys:
            if keys.count(key) > 1:  # count, not a set: keys may be unhashable
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", node.start_mark
                )
        return super().construct_mapping(node, deep)


def account_id(rules: AccountRules) -> str:
    return f"{rules.firm}-{rules.account}"


def rules_path(account_id: str, rules_dir: Path) -> Path:
    match = _ID.fullmatch(account_id)
    if match is None:
        raise ValueError(f"invalid account id {account_id!r}")
    return rules_dir / match[1] / f"{match[2]}.yaml"


def load_account(aid: str, rules_dir: Path) -> AccountRules:
    path = rules_path(aid, rules_dir)
    try:
        # the terminal can write rules/ and the root server reads it (DEC-1); read_regular's
        # ValueError (symlink, not a regular file, too big) already names the file
        text = read_regular(path, RULES_MAX).decode("utf-8")
    except FileNotFoundError:
        raise ValueError(f"unknown account {aid!r}: {path} not found") from None
    except (UnicodeDecodeError, OSError) as e:
        raise ValueError(f"{path}: {e}") from e
    return parse_account(text, aid, path)


def parse_account(text: str, aid: str, path: Path) -> AccountRules:
    """`text` as the rules for `aid`; `path` only names the file in errors."""
    try:
        rules = AccountRules.model_validate(yaml.load(text, _UniqueKeyLoader))
    # ValueError also covers ValidationError and bad YAML dates
    except (yaml.YAMLError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e
    if account_id(rules) != aid:
        raise ValueError(f"{path}: file says {account_id(rules)}, path says {aid}")
    return rules


def list_accounts(rules_dir: Path) -> tuple[str, ...]:
    """ValueError naming `rules_dir` or a `<firm>/` dir that is a symlink: the terminal can
    swap either for a link to a dir only the root server may read (DEC-1)."""
    refuse_symlink(rules_dir)
    paths = list(rules_dir.glob("*/*.yaml"))
    for firm in {p.parent for p in paths}:
        refuse_symlink(firm)
    return tuple(sorted(f"{p.parent.name}-{p.stem}" for p in paths))


def dump_account(rules: AccountRules) -> str:
    return yaml.safe_dump(rules.model_dump(), sort_keys=False)
