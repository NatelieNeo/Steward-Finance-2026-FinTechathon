"""Agent action lifecycle: propose -> policy decision -> execute | await approval | deny.

State machine (agent_actions.status):
    proposed --ALLOW--> executed | failed
    proposed --REQUIRE_APPROVAL--> pending_approval --approve--> executing --> executed | failed
                                                    --reject--> rejected
                                                    --48h----> expired
    proposed --DENY--> denied
Transitions out of pending_approval use a conditional UPDATE so a double-click (or two
devices) can never execute the same approval twice.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from steward.agent.actions import PARAM_MODELS
from steward.agent.policy import (
    ACTION_RISK,
    MONEY_MOVEMENT,
    PROHIBITED,
    AccountInfo,
    BillInfo,
    Decision,
    Outcome,
    PayeeInfo,
    PolicyContext,
    evaluate,
)
from steward.core.money import fmt
from steward.models import (
    Account,
    AgentAction,
    AgentPolicy,
    Alert,
    Bill,
    Budget,
    Dispute,
    Goal,
    Payee,
    SubscriptionState,
    Transaction,
    User,
)
from steward.sandbox import bank
from steward.security import audit
from steward.security.audit import canonical
from steward.services.analysis import Analysis, today_for

APPROVAL_TTL_DAYS = 2


class ActionError(Exception):
    status_code = 400


class NotFound(ActionError):
    status_code = 404


class Conflict(ActionError):
    status_code = 409


class StepUpRequired(ActionError):
    status_code = 401


def action_to_dict(a: AgentAction) -> dict:
    return {
        "id": a.id,
        "action_type": a.action_type,
        "params": a.params,
        "summary": a.summary,
        "amount_cents": a.amount_cents,
        "risk": a.risk,
        "status": a.status,
        "decision": a.decision,
        "reasons": a.decision_reasons,
        "checks": a.checks,
        "requires_step_up": a.requires_step_up,
        "rationale": a.rationale,
        "origin": a.origin,
        "result": a.result,
        "created_on": a.created_on.isoformat() if a.created_on else None,
        "executed_on": a.executed_on.isoformat() if a.executed_on else None,
        "expires_on": a.expires_on.isoformat() if a.expires_on else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


class AgentService:
    def __init__(self, db: Session, user: User) -> None:
        self.db = db
        self.user = user
        self.today = today_for(user)
        policy = db.get(AgentPolicy, user.id)
        if policy is None:
            policy = AgentPolicy(user_id=user.id, tier=1)
            db.add(policy)
            db.flush()
        self.policy = policy
        self.last_was_new = False

    # ----------------------------------------------------------- context
    def build_context(self) -> PolicyContext:
        db, uid = self.db, self.user.id
        accounts = db.execute(select(Account).where(Account.user_id == uid)).scalars().all()
        payees = db.execute(select(Payee).where(Payee.user_id == uid)).scalars().all()
        analysis = Analysis(db, self.user)
        bills = {}
        for b in analysis.open_bills:
            view = analysis.bill_view(b)
            bills[b.id] = BillInfo(b.id, b.payee_id, b.amount_cents, b.status, view["audit"]["status"])
        owned = dict(db.execute(select(Transaction.id, Transaction.amount_cents)
                                .where(Transaction.user_id == uid)).all())
        disputed = set(db.execute(select(Dispute.transaction_id).where(Dispute.user_id == uid)).scalars())
        active_subs = {s.merchant for s in analysis.series if s.kind == "subscription" and s.active}
        month_start = self.today.replace(day=1)
        moved = db.execute(
            select(AgentAction.executed_on, AgentAction.amount_cents).where(
                AgentAction.user_id == uid, AgentAction.status == "executed",
                AgentAction.action_type.in_(MONEY_MOVEMENT), AgentAction.executed_on >= month_start)
        ).all()
        recent = db.execute(select(func.count(AgentAction.id)).where(
            AgentAction.user_id == uid, AgentAction.created_on == self.today,
            AgentAction.risk.in_(["R2", "R3"]), AgentAction.status != "denied")).scalar_one()
        p = self.policy
        return PolicyContext(
            tier=p.tier, frozen=p.frozen,
            per_action_limit_cents=p.per_action_limit_cents, daily_limit_cents=p.daily_limit_cents,
            monthly_limit_cents=p.monthly_limit_cents, floor_cents=p.min_balance_floor_cents,
            step_up_threshold_cents=p.step_up_threshold_cents,
            accounts={a.id: AccountInfo(a.id, a.type, a.balance_cents, a.name) for a in accounts},
            payees={x.id: PayeeInfo(x.id, x.name, x.verified, x.agent_allowlisted) for x in payees},
            bills=bills, owned_txns=owned, disputed_txn_ids=disputed, active_subscriptions=active_subs,
            goal_ids=set(db.execute(select(Goal.id).where(Goal.user_id == uid)).scalars()),
            moved_today_cents=sum(a or 0 for d, a in moved if d == self.today),
            moved_month_cents=sum(a or 0 for _, a in moved),
            actions_today=int(recent),
        )

    # ------------------------------------------------------- normalisation
    def _normalize(self, action_type: str, params: dict) -> tuple[dict, int | None, str]:
        model = PARAM_MODELS[action_type]
        p = model.model_validate(params).model_dump()
        db, uid = self.db, self.user.id
        accounts = db.execute(select(Account).where(Account.user_id == uid)).scalars().all()
        checking = next((a for a in accounts if a.type == "checking"), None)
        savings = next((a for a in accounts if a.type == "savings"), None)
        amount = None
        if action_type == "transfer_to_savings":
            p["from_account_id"] = p["from_account_id"] or (checking.id if checking else None)
            p["to_account_id"] = p["to_account_id"] or (savings.id if savings else None)
            amount = p["amount_cents"]
            src = db.get(Account, p["from_account_id"]) if p["from_account_id"] else None
            dst = db.get(Account, p["to_account_id"]) if p["to_account_id"] else None
            goal = db.get(Goal, p["goal_id"]) if p["goal_id"] else None
            goal_txt = f" for {goal.name}" if goal and goal.user_id == uid else ""
            summary = (f"Move {fmt(amount)} from {src.name if src and src.user_id == uid else 'account'} to "
                       f"{dst.name if dst and dst.user_id == uid else 'account'}{goal_txt}")
        elif action_type == "pay_bill":
            bill = db.get(Bill, p["bill_id"])
            owned = bill is not None and bill.user_id == uid
            p["from_account_id"] = p["from_account_id"] or (checking.id if checking else None)
            if p["amount_cents"] is None:
                p["amount_cents"] = bill.amount_cents if owned else 0
            amount = p["amount_cents"]
            payee = db.get(Payee, bill.payee_id) if owned else None
            summary = f"Pay {fmt(amount)} to {payee.name if payee else 'unknown biller'}" + (
                f" ({bill.period_label} bill, due {bill.due_on:%d %b})" if owned else "")
        elif action_type == "set_budget":
            summary = f"Set {p['category']} budget to {fmt(p['monthly_limit_cents'])}/month"
        elif action_type == "categorize_transaction":
            txn = db.get(Transaction, p["transaction_id"])
            name = txn.merchant if txn is not None and txn.user_id == uid else "transaction"
            summary = f"Recategorise {name} as {p['category']}"
        elif action_type == "create_reminder":
            summary = f"Reminder: {p['title']}"
        elif action_type == "cancel_subscription":
            summary = f"Cancel {p['merchant']} subscription"
        elif action_type == "dispute_charge":
            txn = db.get(Transaction, p["transaction_id"])
            if txn is not None and txn.user_id == uid:
                amount = abs(txn.amount_cents)
                summary = f"Dispute {fmt(amount)} charge from {txn.merchant} on {txn.posted_on:%d %b}"
            else:
                summary = "Dispute a charge"
        else:  # pragma: no cover - guarded by PARAM_MODELS
            summary = action_type
        return p, amount, summary[:255]

    def _key(self, origin: str, action_type: str, params: dict | None, scope: str | None) -> str:
        body = "*" if params is None else canonical(params)
        raw = f"{origin}|{action_type}|{body}|{scope or self.today.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------ propose
    def propose(self, action_type: str, params: dict, *, rationale: str = "", origin: str = "chat",
                idempotency_scope: str | None = None, scope_only: bool = False) -> AgentAction:
        """Validate, evaluate and record an action; execute it if the policy allows.

        Idempotency: the same (origin, type, params, scope) returns the existing action instead
        of creating a second one. With scope_only=True the params are ignored, e.g. "at most one
        savings sweep per week" whatever the amount. If the earlier proposal expired unanswered,
        a fresh one is raised (attempt 2, 3, ...), never two pending at once."""
        params = dict(params or {})
        key_params = None if scope_only else params
        key = self._key(origin, action_type, key_params, idempotency_scope)
        self.last_was_new = False
        existing = self._by_key(key)
        attempt = 1
        while existing is not None and existing.status == "expired" and attempt < 20:
            attempt += 1
            key = self._key(origin, action_type, key_params, f"{idempotency_scope}:attempt:{attempt}")
            existing = self._by_key(key)
        if existing:
            return existing
        self.last_was_new = True

        rationale = (rationale or "")[:1000]
        if action_type in PROHIBITED or action_type not in ACTION_RISK:
            decision = evaluate(action_type, params, self.build_context())
            # The model may invent any name; clip it to the column (PostgreSQL rejects overlong strings).
            action_type = str(action_type)[:40]
            return self._record(action_type, {k: str(v)[:80] for k, v in list(params.items())[:8]}, None,
                                f"Refused: {action_type}", decision, rationale, origin, key)
        try:
            clean, amount, summary = self._normalize(action_type, params)
        except ValidationError as exc:
            reasons = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()][:5]
            decision = Decision(Outcome.DENY, ACTION_RISK[action_type], [])
            action = self._record(action_type, {k: str(v)[:80] for k, v in list(params.items())[:8]}, None,
                                  f"Invalid {action_type} request", decision, rationale, origin, key)
            action.decision_reasons = ["Invalid parameters: " + "; ".join(reasons)]
            return action
        decision = evaluate(action_type, clean, self.build_context())
        action = self._record(action_type, clean, amount, summary, decision, rationale, origin, key)
        if decision.outcome == Outcome.ALLOW:
            self._run(action, approved=False)
        return action

    def _record(self, action_type: str, params: dict, amount: int | None, summary: str, decision: Decision,
                rationale: str, origin: str, key: str) -> AgentAction:
        status = {Outcome.ALLOW: "proposed", Outcome.REQUIRE_APPROVAL: "pending_approval",
                  Outcome.DENY: "denied"}[decision.outcome]
        action = AgentAction(
            user_id=self.user.id, action_type=action_type, params=params, summary=summary, amount_cents=amount,
            risk=decision.risk, status=status, decision=decision.outcome.value, decision_reasons=decision.reasons,
            checks=decision.to_dict()["checks"], requires_step_up=decision.requires_step_up, rationale=rationale,
            origin=origin, idempotency_key=key, created_on=self.today,
            expires_on=self.today + timedelta(days=APPROVAL_TTL_DAYS) if status == "pending_approval" else None,
        )
        self.db.add(action)
        self.db.flush()
        audit.append(self.db, self.user.id, "agent" if origin != "user" else "user", "action.proposed", {
            "action_id": action.id, "type": action_type, "params": params, "amount_cents": amount,
            "origin": origin, "tier": self.policy.tier, "decision": decision.outcome.value,
            "risk": decision.risk, "reasons": decision.reasons,
        })
        return action

    # ------------------------------------------------------------ approve
    def approve(self, action_id: int, *, step_up_ok: bool) -> AgentAction:
        action = self._get(action_id)
        if action.status != "pending_approval":
            raise Conflict(f"Action is {action.status}, not awaiting approval.")
        if action.expires_on and self.today > action.expires_on:
            action.status = "expired"
            raise Conflict("Approval window has expired.")
        if action.requires_step_up and not step_up_ok:
            raise StepUpRequired("Confirm your password to approve this amount.")
        claimed = self.db.execute(
            update(AgentAction).where(AgentAction.id == action.id, AgentAction.status == "pending_approval")
            .values(status="executing", decided_at=datetime.now(timezone.utc))
        ).rowcount
        if claimed != 1:
            raise Conflict("Action was already handled.")
        self.db.refresh(action)
        audit.append(self.db, self.user.id, "user", "action.approved", {"action_id": action.id,
                                                                          "step_up": step_up_ok})
        self._run(action, approved=True)
        return action

    def reject(self, action_id: int, reason: str = "") -> AgentAction:
        action = self._get(action_id)
        claimed = self.db.execute(
            update(AgentAction).where(AgentAction.id == action.id, AgentAction.status == "pending_approval")
            .values(status="rejected", decided_at=datetime.now(timezone.utc))
        ).rowcount
        if claimed != 1:
            raise Conflict(f"Action is {action.status}, not awaiting approval.")
        self.db.refresh(action)
        audit.append(self.db, self.user.id, "user", "action.rejected", {"action_id": action.id,
                                                                          "reason": reason[:200]})
        return action

    def expire_stale(self) -> int:
        stale = self.db.execute(select(AgentAction).where(
            AgentAction.user_id == self.user.id, AgentAction.status == "pending_approval",
            AgentAction.expires_on < self.today)).scalars().all()
        for a in stale:
            a.status = "expired"
            audit.append(self.db, self.user.id, "system", "action.expired", {"action_id": a.id})
        return len(stale)

    def _by_key(self, key: str) -> AgentAction | None:
        return self.db.execute(select(AgentAction).where(
            AgentAction.user_id == self.user.id, AgentAction.idempotency_key == key)).scalar_one_or_none()

    def _get(self, action_id: int) -> AgentAction:
        action = self.db.get(AgentAction, action_id)
        if action is None or action.user_id != self.user.id:  # never reveal other users' actions
            raise NotFound("Action not found.")
        return action

    # ------------------------------------------------------------ execute
    def _run(self, action: AgentAction, *, approved: bool) -> None:
        # Re-check at execution time: balances, limits, tier or kill switch may have changed.
        decision = evaluate(action.action_type, action.params, self.build_context(), approved_by_user=approved)
        if decision.outcome != Outcome.ALLOW:
            action.status = "failed"
            action.result = {"error": "Policy re-check failed at execution time.", "reasons": decision.reasons}
            audit.append(self.db, self.user.id, "system", "action.failed",
                         {"action_id": action.id, "reasons": decision.reasons})
            return
        try:
            result = self._execute(action)
        except (bank.LedgerError, ValueError) as exc:
            action.status = "failed"
            action.result = {"error": str(exc)}
            audit.append(self.db, self.user.id, "system", "action.failed", {"action_id": action.id,
                                                                              "error": str(exc)})
            return
        action.status = "executed"
        action.executed_on = self.today
        action.result = result
        audit.append(self.db, self.user.id, "agent", "action.executed", {
            "action_id": action.id, "type": action.action_type, "amount_cents": action.amount_cents,
            "approved_by_user": approved, "result": result})

    def _execute(self, action: AgentAction) -> dict:
        p, db, uid, today = action.params, self.db, self.user.id, self.today
        t = action.action_type
        if t == "categorize_transaction":
            txn = db.get(Transaction, p["transaction_id"])
            old = txn.category
            txn.category, txn.category_source, txn.category_confidence = p["category"], "agent", 1.0
            return {"transaction_id": txn.id, "from": old, "to": p["category"]}
        if t == "set_budget":
            budget = db.execute(select(Budget).where(Budget.user_id == uid, Budget.category == p["category"])
                                ).scalar_one_or_none()
            if budget is None:
                budget = Budget(user_id=uid, category=p["category"], monthly_limit_cents=0, created_by="agent")
                db.add(budget)
            budget.monthly_limit_cents = p["monthly_limit_cents"]
            budget.updated_at = datetime.now(timezone.utc)
            db.flush()
            return {"budget_id": budget.id, "category": p["category"], "limit_cents": p["monthly_limit_cents"]}
        if t == "create_reminder":
            alert = Alert(user_id=uid, kind="reminder", severity=p["severity"], title=p["title"], body=p["body"],
                          dedupe_key=f"reminder:{action.id}", created_on=today)
            db.add(alert)
            db.flush()
            return {"alert_id": alert.id}
        if t == "transfer_to_savings":
            # Row locks (PostgreSQL) so concurrent debits cannot both pass the funds check.
            src = db.get(Account, p["from_account_id"], with_for_update=True, populate_existing=True)
            dst = db.get(Account, p["to_account_id"], with_for_update=True, populate_existing=True)
            out, inn = bank.transfer(db, src, dst, p["amount_cents"], today, f"STEWARD #{action.id}",
                                     created_by="agent")
            if p.get("goal_id"):
                goal = db.get(Goal, p["goal_id"])
                goal.saved_cents += p["amount_cents"]
            return {"debit_txn": out.id, "credit_txn": inn.id, "new_source_balance_cents": src.balance_cents}
        if t == "pay_bill":
            bill = db.get(Bill, p["bill_id"])
            payee = db.get(Payee, bill.payee_id)
            src = db.get(Account, p["from_account_id"], with_for_update=True, populate_existing=True)
            txn = bank.pay_biller(db, src, payee, p["amount_cents"], today, bill.period_label, created_by="agent")
            if p["amount_cents"] >= bill.amount_cents:
                bill.status, bill.paid_on, bill.paid_by = "paid", today, "agent"
            return {"txn_id": txn.id, "bill_id": bill.id, "paid_on": today.isoformat(),
                    "on_time": today <= bill.due_on}
        if t == "cancel_subscription":
            state = db.execute(select(SubscriptionState).where(
                SubscriptionState.user_id == uid, SubscriptionState.merchant == p["merchant"])).scalar_one_or_none()
            if state is None:
                state = SubscriptionState(user_id=uid, merchant=p["merchant"])
                db.add(state)
            state.status = "cancelled"
            state.updated_at = datetime.now(timezone.utc)
            db.flush()
            return {"merchant": p["merchant"], "status": "cancelled",
                    "note": "Sandbox merchant confirmed cancellation; no further charges will be posted."}
        if t == "dispute_charge":
            txn = db.get(Transaction, p["transaction_id"])
            dispute = Dispute(user_id=uid, transaction_id=txn.id, reason=p["reason"])
            db.add(dispute)
            db.flush()
            account = db.get(Account, txn.account_id)
            credit = bank.post(db, account, on=today, description=f"PROVISIONAL CREDIT DISPUTE {dispute.id}",
                               amount_cents=abs(txn.amount_cents), kind="refund", created_by="agent",
                               category=txn.category, merchant=txn.merchant)
            return {"dispute_id": dispute.id, "provisional_credit_txn": credit.id,
                    "amount_cents": abs(txn.amount_cents)}
        raise ValueError(f"no executor for {t}")
