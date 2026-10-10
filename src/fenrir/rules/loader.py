"""Account id <-> rules file. The only rules code that touches disk."""

import re
from pathlib import Path

import yaml

from fenrir.rules.schema import AccountRules

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


def load_account(account_id: str, rules_dir: Path) -> AccountRules:
    path = rules_path(account_id, rules_dir)
    try:
        rules = AccountRules.model_validate(yaml.load(path.read_text(), _UniqueKeyLoader))
    except FileNotFoundError:
        raise ValueError(f"unknown account {account_id!r}: {path} not found") from None
    # ValueError also covers ValidationError, bad YAML dates and non-UTF-8 files
    except (yaml.YAMLError, ValueError, OSError) as e:
        raise ValueError(f"{path}: {e}") from e
    found = f"{rules.firm}-{rules.account}"
    if found != account_id:
        raise ValueError(f"{path}: file says {found}, path says {account_id}")
    return rules


def list_accounts(rules_dir: Path) -> tuple[str, ...]:
    return tuple(sorted(f"{p.parent.name}-{p.stem}" for p in rules_dir.glob("*/*.yaml")))


def dump_account(rules: AccountRules) -> str:
    return yaml.safe_dump(rules.model_dump(), sort_keys=False)
