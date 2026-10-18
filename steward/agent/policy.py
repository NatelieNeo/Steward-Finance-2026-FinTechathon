"""Deterministic policy engine: the only gate between the agent and the user's money.

The language model never executes anything. Every action, whether it comes from the daily
cycle, the chat agent or a one-click insight, is evaluated here against the user's
permission tier and guardrails. The engine is pure (no I/O) so it is exhaustively testable.

Outcome rules
  1. Prohibited action classes are always DENIED, at every tier.
  2. Any failed "deny" check (ownership, funds, floor, kill switch, tier...) -> DENY.
  3. Otherwise, if the tier requires a human for this risk class, or any "approval"
     check fails (limits, unusual amount, audit findings...) -> REQUIRE_APPROVAL.
  4. Otherwise -> ALLOW (execute autonomously).
After a human approves, the action is re-evaluated at execution time with approval checks
waived but every deny check re-run (closing the time-of-check/time-of-use gap).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Outcome(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


TIERS = {
    0: {"name": "Observer", "summary": "Read-only. Analyses your money and sends alerts; changes nothing."},
    1: {"name": "Organizer", "summary": "Auto-applies non-monetary changes (budgets, categories, reminders). "
                                        "May suggest moving money between your own accounts, for your approval."},
    2: {"name": "Assistant", "summary": "Also prepares bill payments to verified billers, cancellations and "
                                        "disputes. Every money movement waits for your approval."},
    3: {"name": "Autopilot", "summary": "Executes routine money movements by itself inside your limits "
                                        "(savings sweeps, clean bills to allowlisted billers). Anything unusual "
                                        "still comes to you."},
}

# Risk classes: R1 non-monetary write, R2 money between the user's own accounts,
# R3 money or commitments leaving to third parties, R4 prohibited.
ACTION_RISK = {
    "categorize_transaction": "R1",
    "set_budget": "R1",
    "create_reminder": "R1",
    "transfer_to_savings": "R2",
    "pay_bill": "R3",
    "cancel_subscription": "R3",
    "dispute_charge": "R3",
}
PROHIBITED = {
    "add_payee": "Adding payees changes who can receive your money; only you can do that.",
    "external_transfer": "Sending money to people or new accounts is never delegated to the agent.",
    "change_agent_limits": "The agent cannot change its own permissions or limits.",
    "buy_investment": "Trading or investing is outside this assistant's mandate.",
    "sell_investment": "Trading or investing is outside this assistant's mandate.",
    "apply_for_credit": "Taking on credit is never delegated to the agent.",
    "close_account": "Closing accounts is never delegated to the agent.",
    "withdraw_cash": "Cash withdrawals are never delegated to the agent.",
}
MONEY_MOVEMENT = {"transfer_to_savings", "pay_bill"}
AUTONOMOUS_R3 = {"pay_bill"}  # the only third-party action Autopilot may run by itself
HARD_CEILING_CENTS = 1_000_000  # S$10,000: no single agent action above this, whatever the settings

_A, _R, _D = Outcome.ALLOW, Outcome.REQUIRE_APPROVAL, Outcome.DENY
TIER_MATRIX = {
    0: {"R1": _D, "R2": _D, "R3": _D},
    1: {"R1": _A, "R2": _R, "R3": _D},
    2: {"R1": _A, "R2": _R, "R3": _R},
    3: {"R1": _A, "R2": _A, "R3": _A},
}


@dataclass(frozen=True)
class AccountInfo:
    id: int
    type: str
    balance_cents: int
    name: str = ""


@dataclass(frozen=True)
class PayeeInfo:
    id: int
    name: str
    verified: bool
    allowlisted: bool


@dataclass(frozen=True)
class BillInfo:
    id: int
    payee_id: int
    amount_cents: int
    status: str
    audit_status: str  # ok | ok_with_notes | review | dispute


@dataclass
class PolicyContext:
    tier: int
    frozen: bool
    per_action_limit_cents: int
    daily_limit_cents: int
    monthly_limit_cents: int
    floor_cents: int
    step_up_threshold_cents: int
    accounts: dict[int, AccountInfo] = field(default_factory=dict)
    payees: dict[int, PayeeInfo] = field(default_factory=dict)
    bills: dict[int, BillInfo] = field(default_factory=dict)
    owned_txns: dict[int, int] = field(default_factory=dict)  # txn id -> amount_cents
    disputed_txn_ids: set[int] = field(default_factory=set)
    active_subscriptions: set[str] = field(default_factory=set)
    goal_ids: set[int] = field(default_factory=set)
    moved_today_cents: int = 0
    moved_month_cents: int = 0
    actions_today: int = 0
    max_actions_per_day: int = 15


@dataclass
class Check:
    name: str
    passed: bool
    on_fail: str  # deny | approval
    detail: str


@dataclass
class Decision:
    outcome: Outcome
    risk: str
    checks: list[Check]
    requires_step_up: bool = False

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {"outcome": self.outcome.value, "risk": self.risk, "requires_step_up": self.requires_step_up,
                "reasons": self.reasons, "checks": [asdict(c) for c in self.checks]}


def tier_baseline(tier: int, action_type: str) -> Outcome:
    risk = ACTION_RISK[action_type]
    base = TIER_MATRIX.get(tier, TIER_MATRIX[0])[risk]
    if tier == 3 and risk == "R3" and action_type not in AUTONOMOUS_R3:
        base = Outcome.REQUIRE_APPROVAL
    return base


def _money(amount: int) -> str:
    return f"S${amount / 100:,.2f}"


def evaluate(action_type: str, params: dict, ctx: PolicyContext, *, approved_by_user: bool = False) -> Decision:
    if action_type in PROHIBITED:
        return Decision(Outcome.DENY, "R4", [Check("prohibited_action", False, "deny", PROHIBITED[action_type])])
    if action_type not in ACTION_RISK:
        return Decision(Outcome.DENY, "R4", [Check("known_action", False, "deny",
                                                   f"'{action_type}' is not an action the agent supports.")])
    risk = ACTION_RISK[action_type]
    checks: list[Check] = []

    def add(name: str, passed: bool, on_fail: str, detail: str) -> None:
        checks.append(Check(name, passed, on_fail, detail if not passed else "ok"))

    add("kill_switch", not ctx.frozen, "deny", "The agent is frozen (kill switch is on).")
    base = tier_baseline(ctx.tier, action_type)
    add("tier_permits", base != Outcome.DENY, "deny",
        f"Tier {ctx.tier} ({TIERS.get(ctx.tier, TIERS[0])['name']}) does not allow {risk} actions "
        f"like {action_type}.")

    amount = int(params.get("amount_cents") or 0)
    source: AccountInfo | None = None

    if action_type == "categorize_transaction":
        add("owns_transaction", params.get("transaction_id") in ctx.owned_txns, "deny",
            "Transaction not found in your accounts.")

    elif action_type == "transfer_to_savings":
        source = ctx.accounts.get(params.get("from_account_id"))
        dest = ctx.accounts.get(params.get("to_account_id"))
        add("owns_source_account", source is not None and source.type in {"checking", "savings"}, "deny",
            "Source account is not one of your deposit accounts.")
        add("owns_destination_account", dest is not None and dest.type == "savings", "deny",
            "Destination must be one of your own savings accounts.")
        add("distinct_accounts", source is None or dest is None or source.id != dest.id, "deny",
            "Source and destination are the same account.")
        if params.get("goal_id") is not None:
            add("owns_goal", params["goal_id"] in ctx.goal_ids, "deny", "Savings goal not found.")

    elif action_type == "pay_bill":
        bill = ctx.bills.get(params.get("bill_id"))
        source = ctx.accounts.get(params.get("from_account_id"))
        add("owns_bill", bill is not None, "deny", "Bill not found in your account.")
        add("owns_source_account", source is not None and source.type == "checking", "deny",
            "Bills can only be paid from your own checking account.")
        if bill:
            payee = ctx.payees.get(bill.payee_id)
            add("bill_payable", bill.status in {"open", "overdue"}, "deny", f"Bill is already {bill.status}.")
            add("payee_verified", bool(payee and payee.verified), "deny", "Biller is not verified.")
            add("payee_allowlisted", bool(payee and payee.allowlisted), "approval",
                f"{payee.name if payee else 'Biller'} is not on the agent's allowlist; a human must approve.")
            add("not_overpaying", amount <= bill.amount_cents, "deny",
                f"Cannot pay {_money(amount)}: the statement is {_money(bill.amount_cents)}.")
            add("matches_statement", amount == bill.amount_cents, "approval",
                f"Amount {_money(amount)} differs from the statement ({_money(bill.amount_cents)}).")
            add("bill_audit_clean", bill.audit_status in {"ok", "ok_with_notes"}, "approval",
                "The bill audit found items that need a human look (spike, new charges or fees).")

    elif action_type == "cancel_subscription":
        add("subscription_active", params.get("merchant") in ctx.active_subscriptions, "deny",
            f"No active subscription to {params.get('merchant')} was found.")

    elif action_type == "dispute_charge":
        txn_amount = ctx.owned_txns.get(params.get("transaction_id"))
        add("owns_transaction", txn_amount is not None, "deny", "Transaction not found in your accounts.")
        add("is_debit", txn_amount is not None and txn_amount < 0, "deny", "Only charges (debits) can be disputed.")
        add("not_already_disputed", params.get("transaction_id") not in ctx.disputed_txn_ids, "deny",
            "This charge is already disputed.")

    if action_type in MONEY_MOVEMENT:
        add("positive_amount", amount > 0, "deny", "Amount must be positive.")
        add("hard_ceiling", amount <= HARD_CEILING_CENTS, "deny",
            f"Single agent actions are capped at {_money(HARD_CEILING_CENTS)} regardless of settings.")
        if source is not None:
            add("sufficient_funds", source.balance_cents >= amount, "deny",
                f"{source.name or 'Source account'} has insufficient funds.")
            add("balance_floor", source.balance_cents - amount >= ctx.floor_cents, "deny",
                f"Would leave {_money(source.balance_cents - amount)}, below your "
                f"{_money(ctx.floor_cents)} safety floor.")
        add("per_action_limit", amount <= ctx.per_action_limit_cents, "approval",
            f"{_money(amount)} is above the per-action limit of {_money(ctx.per_action_limit_cents)}.")
        add("daily_limit", ctx.moved_today_cents + amount <= ctx.daily_limit_cents, "approval",
            f"Would exceed today's agent limit of {_money(ctx.daily_limit_cents)} "
            f"({_money(ctx.moved_today_cents)} already moved).")
        add("monthly_limit", ctx.moved_month_cents + amount <= ctx.monthly_limit_cents, "approval",
            f"Would exceed this month's agent limit of {_money(ctx.monthly_limit_cents)}.")

    if risk != "R1":
        add("velocity", ctx.actions_today < ctx.max_actions_per_day, "approval",
            "Unusually many agent money actions today; a human should take a look.")

    failed = [c for c in checks if not c.passed]
    if any(c.on_fail == "deny" for c in failed):
        outcome = Outcome.DENY
    elif approved_by_user:
        outcome = Outcome.ALLOW
    elif base == Outcome.REQUIRE_APPROVAL or any(c.on_fail == "approval" for c in failed):
        outcome = Outcome.REQUIRE_APPROVAL
    else:
        outcome = Outcome.ALLOW
    step_up = outcome == Outcome.REQUIRE_APPROVAL and amount >= ctx.step_up_threshold_cents > 0
    return Decision(outcome, risk, checks, requires_step_up=step_up)
