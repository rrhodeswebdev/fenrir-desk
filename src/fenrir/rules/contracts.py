"""Contract limit in mini-equivalents (a micro counts as ContractSpec.minis, 0.1).

contracts.micros_per_mini is not used: micro sizes come from ContractSpec.minis (same 10:1).
"""

from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState


def max_minis(rules: AccountRules, state: AccountState) -> int:
    """Funded scaling tier from the balance at the last session end; else the flat limit."""
    tiers = rules.funded.scaling
    if state.phase != "funded" or tiers is None:
        return rules.contracts.max_minis
    profit = max(0.0, state.day_start_balance)
    return [t for t in tiers if t.from_profit <= profit][-1].max_minis


def contract_limit(limit: int, held_minis: float, add_minis: float) -> str | None:
    total = round(held_minis + add_minis, 6)  # 0.1 steps drift in floats
    if total > limit:
        return f"contract limit: {total:g} mini-equivalents exceeds max_minis {limit}"
    return None
