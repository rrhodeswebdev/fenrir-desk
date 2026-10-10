"""Account id <-> rules file. The only rules code that touches disk."""

import re
from pathlib import Path

import yaml

from fenrir.rules.schema import AccountRules

_ID = re.compile(r"([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)")


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
        rules = AccountRules.model_validate(yaml.safe_load(path.read_text()))
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
