"""An account type: rule functions built from YAML, pass/payout gates and the strategy view."""

from datetime import date

from fenrir.rules import schema
from fenrir.rules.consistency import consistent
from fenrir.rules.daily_loss import daily_loss_limit
from fenrir.rules.drawdown import max_drawdown
from fenrir.rules.flat_by import flat_by
from fenrir.rules.ledger import book
from fenrir.rules.microscalp import microscalp
from fenrir.rules.schema import AccountRules
from fenrir.rules.state import AccountState, AccountView, Breach, Event, Phase, Rule


def phase_rules(rules: AccountRules, phase: Phase) -> schema.Phase:
    p = rules.evaluation if phase == "evaluation" else rules.funded
    if p is None:
        raise ValueError(f"{rules.firm}-{rules.account} has no evaluation phase")
    return p


def initial_state(rules: AccountRules, phase: Phase, day: date) -> AccountState:
    return AccountState(
        phase=phase,
        day=day,
        balance=0.0,
        open_pnl=0.0,
        position=0,
        peak=0.0,
        floor=-phase_rules(rules, phase).max_drawdown.amount,
        day_start_balance=0.0,
        closed_days=(),
        traded_today=False,
        halted_today=False,
        possible_microscalp_pnl=0.0,
    )


def with_dll(rules: AccountRules, dll_on: bool) -> AccountRules:
    """The rules as traded: the DLL amount applies only when the user turned the DLL on."""
    phases = {
        name: p
        for name, p in (("evaluation", rules.evaluation), ("funded", rules.funded))
        if p is not None
    }
    if dll_on:
        missing = [name for name, p in phases.items() if p.daily_loss_limit is None]
        if missing:
            raise ValueError(
                f"{rules.firm}-{rules.account}: DLL is on but the rules file has no "
                f"daily_loss_limit amount for {', '.join(missing)}"
            )
        return rules
    return rules.model_copy(
        update={name: p.model_copy(update={"daily_loss_limit": None}) for name, p in phases.items()}
    )


def build_rules(rules: AccountRules, phase: Phase) -> tuple[Rule, ...]:
    p = phase_rules(rules, phase)
    dll = (daily_loss_limit(p.daily_loss_limit),) if p.daily_loss_limit is not None else ()
    scalp = (microscalp,) if rules.prohibited.microscalp is not None else ()
    return (
        book,
        max_drawdown(p.max_drawdown),
        flat_by(rules.session.flat_by, rules.session.tz),
        *dll,
        *scalp,
    )


def apply(rule_fns: tuple[Rule, ...], state: AccountState, event: Event) -> AccountState | Breach:
    for fn in rule_fns:
        result = fn(state, event)
        if isinstance(result, Breach):
            return result
        state = result
    return state


def day_pnls(state: AccountState) -> tuple[float, ...]:
    today = (state.balance - state.day_start_balance,) if state.traded_today else ()
    return tuple(pnl for _, pnl in state.closed_days) + today


def _consistent(p: schema.Phase, days: tuple[float, ...]) -> bool:
    return p.consistency is None or consistent(days, p.consistency.max_day_pct)


def passed(state: AccountState, rules: AccountRules) -> bool:
    e = rules.evaluation
    if state.phase != "evaluation" or e is None or state.position != 0:
        return False
    days = day_pnls(state)
    return (
        state.balance >= e.profit_target
        and len(days) >= e.min_trading_days
        and _consistent(e, days)
    )


def payout(state: AccountState, rules: AccountRules) -> float | None:
    """First payout only (max_payouts is not modelled): balance share, capped, then split."""
    f = rules.funded
    p = f.payout  # never None: the schema requires it on funded
    if state.phase != "funded" or state.position != 0 or state.balance <= 0:
        return None
    days = tuple(pnl for _, pnl in state.closed_days)  # completed sessions only
    if sum(d >= p.min_day_profit for d in days) < p.min_days or not _consistent(f, days):
        return None
    request = state.balance * p.max_pct_of_profit / 100
    if p.max_amount is not None:
        request = min(request, p.max_amount)
    if request < p.min_profit:
        return None
    return request * p.split_pct / 100


def view(state: AccountState, rules: AccountRules) -> AccountView:
    p = phase_rules(rules, state.phase)
    today = state.balance - state.day_start_balance + state.open_pnl
    return AccountView(
        phase=state.phase,
        drawdown_remaining=state.balance + state.open_pnl - state.floor,
        daily_loss_remaining=None if p.daily_loss_limit is None else p.daily_loss_limit + today,
        today_pnl=today,
        target_remaining=None
        if (t := getattr(p, "profit_target", None)) is None
        else max(0.0, t - state.balance),
    )
