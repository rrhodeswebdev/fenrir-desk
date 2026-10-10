from pathlib import Path

import pytest

from fenrir.rules.loader import list_accounts, load_account

RULES = Path(__file__).parents[2] / "rules"


def test_rules_dir_has_files():
    assert list_accounts(RULES)


@pytest.mark.parametrize("account", list_accounts(RULES))
def test_every_rules_file_loads(account):
    load_account(account, RULES)
