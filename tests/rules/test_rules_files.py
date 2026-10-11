from datetime import date, timedelta
from pathlib import Path

import pytest

from fenrir.rules.account import payout
from fenrir.rules.loader import list_accounts, load_account, rules_path

RULES = Path(__file__).parents[2] / "rules"

# From Lucid's pages: profit where the first payout opens, the buffer it may not dip into
# (Pro, Daily) and the cap (None = no cap). Lucid's request at profit P is min(P - buffer, cap).
FIRST_PAYOUT = {
    "lucid-pro-25k": (1600, 1100, 1000),
    "lucid-pro-50k": (2600, 2100, 2000),
    "lucid-pro-100k": (3600, 3100, 2500),
    "lucid-pro-150k": (5100, 4600, 3000),
    "lucid-direct-25k": (1500, 0, 1000),
    "lucid-direct-50k": (3000, 0, 2000),
    "lucid-direct-100k": (6000, 0, 2500),
    "lucid-direct-150k": (9000, 0, 3000),
    "lucid-daily-25k": (1600, 1100, None),
    "lucid-daily-50k": (2600, 2100, None),
    "lucid-daily-100k": (3600, 3100, None),
    "lucid-daily-150k": (5100, 4600, None),
}


def test_rules_dir_has_files():
    assert list_accounts(RULES)


@pytest.mark.parametrize("account", list_accounts(RULES))
def test_every_rules_file_loads(account):
    load_account(account, RULES)


@pytest.mark.parametrize("account", FIRST_PAYOUT)
def test_first_payout_matches_lucid(account, make_state):
    if not rules_path(account, RULES).exists():
        pytest.skip("file lands with #116 / #117")  # shortcut: drop once both are merged
    rules = load_account(account, RULES)
    point, buffer, cap = FIRST_PAYOUT[account]

    def paid(profit):  # funded, flat, ten even days: consistency always holds
        days = tuple((date(2026, 10, 1) + timedelta(i), profit / 10) for i in range(10))
        return payout(make_state(phase="funded", balance=profit, closed_days=days), rules)

    assert paid(point - 0.01) is None
    above = point + 3  # the stand-ins open within $3 of Lucid's point
    lucid = min(above - buffer, cap or above)
    assert paid(above) is not None
    assert paid(above) <= lucid * rules.funded.payout.split_pct / 100
