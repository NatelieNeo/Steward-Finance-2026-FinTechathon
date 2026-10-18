"""The policy engine is the security boundary for every agent action: test it exhaustively."""

import itertools

import pytest

from steward.agent.policy import (
    ACTION_RISK,
    HARD_CEILING_CENTS,
    PROHIBITED,
    AccountInfo,
    BillInfo,
    Outcome,
    PayeeInfo,
    PolicyContext,
    evaluate,
)

A, R, D = Outcome.ALLOW, Outcome.REQUIRE_APPROVAL, Outcome.DENY


def ctx(tier=3, **kw) -> PolicyContext:
    base = dict(
        tier=tier, frozen=False, per_action_limit_cents=50_000, daily_limit_cents=100_000,
        monthly_limit_cents=300_000, floor_cents=100_000, step_up_threshold_cents=30_000,
        accounts={1: AccountInfo(1, "checking", 400_000, "Everyday"), 2: AccountInfo(2, "savings", 700_000, "Savings"),
                  9: AccountInfo(9, "credit", -50_000, "Card")},
        payees={10: PayeeInfo(10, "StarHub", True, True), 11: PayeeInfo(11, "Landlord", True, False),
                12: PayeeInfo(12, "Unverified Co", False, True)},
        bills={100: BillInfo(100, 10, 3990, "open", "ok"), 101: BillInfo(101, 11, 150_000, "open", "ok"),
               102: BillInfo(102, 10, 18_995, "open", "review"), 103: BillInfo(103, 10, 3990, "paid", "ok"),
               104: BillInfo(104, 12, 2000, "open", "ok")},
        owned_txns={500: -8990, 501: 520_000}, disputed_txn_ids=set(), active_subscriptions={"Netflix"},
        goal_ids={7},
    )
    base.update(kw)
    return PolicyContext(**base)


SAMPLES = {
    "categorize_transaction": {"transaction_id": 500, "category": "Shopping"},
    "set_budget": {"category": "Dining", "monthly_limit_cents": 40_000},
    "create_reminder": {"title": "Check bill", "body": "", "severity": "info"},
    "transfer_to_savings": {"amount_cents": 20_000, "from_account_id": 1, "to_account_id": 2, "goal_id": 7},
    "pay_bill": {"bill_id": 100, "amount_cents": 3990, "from_account_id": 1},
    "cancel_subscription": {"merchant": "Netflix", "reason": ""},
    "dispute_charge": {"transaction_id": 500, "reason": "dup"},
}

# The full tier matrix for clean, in-limit requests.
EXPECTED = {
    0: dict.fromkeys(SAMPLES, D),
    1: {"categorize_transaction": A, "set_budget": A, "create_reminder": A, "transfer_to_savings": R,
        "pay_bill": D, "cancel_subscription": D, "dispute_charge": D},
    2: {"categorize_transaction": A, "set_budget": A, "create_reminder": A, "transfer_to_savings": R,
        "pay_bill": R, "cancel_subscription": R, "dispute_charge": R},
    3: {"categorize_transaction": A, "set_budget": A, "create_reminder": A, "transfer_to_savings": A,
        "pay_bill": A, "cancel_subscription": R, "dispute_charge": R},
}


@pytest.mark.parametrize("tier, action", list(itertools.product(range(4), SAMPLES)))
def test_tier_matrix(tier, action):
    assert evaluate(action, SAMPLES[action], ctx(tier)).outcome == EXPECTED[tier][action]


@pytest.mark.parametrize("action", sorted(PROHIBITED))
@pytest.mark.parametrize("tier", range(4))
def test_prohibited_actions_denied_at_every_tier(action, tier):
    d = evaluate(action, {"amount_cents": 1}, ctx(tier))
    assert d.outcome == D and d.risk == "R4"


def test_unknown_action_denied():
    assert evaluate("rm_rf", {}, ctx()).outcome == D


@pytest.mark.parametrize("action", sorted(ACTION_RISK))
def test_kill_switch_denies_everything(action):
    d = evaluate(action, SAMPLES[action], ctx(3, frozen=True))
    assert d.outcome == D and "kill switch" in " ".join(d.reasons)


def test_kill_switch_beats_user_approval():
    d = evaluate("transfer_to_savings", SAMPLES["transfer_to_savings"], ctx(3, frozen=True), approved_by_user=True)
    assert d.outcome == D


class TestMoneyGuardrails:
    def tr(self, amount, **kw):
        return evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "amount_cents": amount}, ctx(3, **kw))

    def test_per_action_limit_escalates(self):
        d = self.tr(50_001)
        assert d.outcome == R and any("per-action" in r for r in d.reasons)

    def test_daily_and_monthly_limits_escalate(self):
        assert self.tr(20_000, moved_today_cents=90_000).outcome == R
        assert self.tr(20_000, moved_month_cents=290_000).outcome == R

    def test_balance_floor_denies(self):
        d = self.tr(40_000, accounts={1: AccountInfo(1, "checking", 130_000), 2: AccountInfo(2, "savings", 0)})
        assert d.outcome == D and any("floor" in r for r in d.reasons)

    def test_insufficient_funds_denies(self):
        d = self.tr(40_000, accounts={1: AccountInfo(1, "checking", 10_000), 2: AccountInfo(2, "savings", 0)},
                    floor_cents=0)
        assert d.outcome == D

    def test_hard_ceiling_denies_even_with_huge_limits(self):
        d = self.tr(HARD_CEILING_CENTS + 1, per_action_limit_cents=10**9, daily_limit_cents=10**9,
                    monthly_limit_cents=10**9, accounts={1: AccountInfo(1, "checking", 10**9),
                                                         2: AccountInfo(2, "savings", 0)})
        assert d.outcome == D

    def test_cannot_transfer_to_a_non_savings_or_foreign_account(self):
        for dest in (9, 1, 999):
            d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "to_account_id": dest}, ctx(3))
            assert d.outcome == D, dest

    def test_foreign_goal_denied(self):
        d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "goal_id": 8}, ctx(3))
        assert d.outcome == D

    def test_zero_amount_denied(self):
        assert self.tr(0).outcome == D

    def test_velocity_escalates(self):
        assert self.tr(10_000, actions_today=15).outcome == R

    def test_step_up_flag_for_large_approvals(self):
        d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "amount_cents": 40_000}, ctx(2))
        assert d.outcome == R and d.requires_step_up
        assert not evaluate("transfer_to_savings", SAMPLES["transfer_to_savings"], ctx(2)).requires_step_up

    def test_user_approval_waives_soft_limits_only(self):
        assert self.tr(60_000).outcome == R
        d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "amount_cents": 60_000}, ctx(3),
                     approved_by_user=True)
        assert d.outcome == A
        d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "amount_cents": 350_000}, ctx(3),
                     approved_by_user=True)
        assert d.outcome == D  # the floor still applies after approval


class TestBillPayments:
    def pay(self, bill_id, amount=None, tier=3, **kw):
        params = {"bill_id": bill_id, "amount_cents": amount, "from_account_id": 1}
        return evaluate("pay_bill", params, ctx(tier, **kw))

    def test_clean_allowlisted_bill_autopays_on_tier3(self):
        assert self.pay(100, 3990).outcome == A

    def test_not_allowlisted_needs_approval(self):
        assert self.pay(101, 150_000, per_action_limit_cents=200_000, daily_limit_cents=200_000).outcome == R

    def test_audit_findings_need_approval(self):
        d = self.pay(102, 18_995)
        assert d.outcome == R and any("audit" in r for r in d.reasons)

    def test_partial_payment_needs_approval_overpayment_denied(self):
        assert self.pay(100, 2000).outcome == R
        assert self.pay(100, 5000).outcome == D

    def test_paid_unknown_or_unverified_bills_denied(self):
        assert self.pay(103, 3990).outcome == D
        assert self.pay(999, 3990).outcome == D
        assert self.pay(104, 2000).outcome == D

    def test_bills_only_from_checking(self):
        d = evaluate("pay_bill", {"bill_id": 100, "amount_cents": 3990, "from_account_id": 2}, ctx(3))
        assert d.outcome == D


class TestOtherActions:
    def test_dispute_rules(self):
        assert evaluate("dispute_charge", {"transaction_id": 501}, ctx(2)).outcome == D  # a credit
        assert evaluate("dispute_charge", {"transaction_id": 999}, ctx(2)).outcome == D  # not owned
        assert evaluate("dispute_charge", {"transaction_id": 500}, ctx(2, disputed_txn_ids={500})).outcome == D

    def test_cancel_requires_active_subscription(self):
        assert evaluate("cancel_subscription", {"merchant": "Spotify"}, ctx(2)).outcome == D

    def test_categorize_foreign_transaction_denied(self):
        assert evaluate("categorize_transaction", {"transaction_id": 999, "category": "Dining"}, ctx(1)).outcome == D

    def test_every_decision_explains_itself(self):
        d = evaluate("transfer_to_savings", {**SAMPLES["transfer_to_savings"], "amount_cents": 60_000}, ctx(3))
        assert d.reasons and all(isinstance(r, str) and r for r in d.reasons)
        assert {c.name for c in d.checks} >= {"kill_switch", "tier_permits", "balance_floor", "per_action_limit"}
