"""The agent's daily cycle: a deterministic planner that looks at the user's finances and
proposes actions. It plans *within* the user's tier (it does not propose actions the tier
can never allow; it raises an alert instead), and every proposal still goes through the
policy engine, so a planning bug cannot exceed the user's permissions."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from steward.agent.policy import Outcome, tier_baseline
from steward.agent.service import AgentService
from steward.core.budgeting import suggest_budgets
from steward.core.money import fmt, month_key
from steward.models import Alert, User
from steward.security import audit
from steward.services.analysis import Analysis

PAY_WINDOW_DAYS = 3
MIN_SWEEP_CENTS = 5_000


def _alert(db: Session, user: User, today: date, kind: str, severity: str, title: str, body: str, key: str) -> bool:
    if db.execute(select(Alert.id).where(Alert.user_id == user.id, Alert.dedupe_key == key)).first():
        return False
    db.add(Alert(user_id=user.id, kind=kind, severity=severity, title=title[:160], body=body,
                 dedupe_key=key[:120], created_on=today))
    try:
        db.flush()
    except IntegrityError:  # pragma: no cover - concurrent duplicate
        db.rollback()
        return False
    return True


def run_cycle(db: Session, user: User) -> dict:
    svc = AgentService(db, user)
    policy = svc.policy
    today = svc.today
    report = {"date": today.isoformat(), "tier": policy.tier, "frozen": policy.frozen, "alerts": 0,
              "proposals": []}
    report["expired"] = svc.expire_stale()
    if policy.frozen:
        audit.append(db, user.id, "agent", "agent.cycle_skipped", {"reason": "kill switch on"})
        return report

    analysis = Analysis(db, user)

    def can(action_type: str) -> bool:
        return tier_baseline(policy.tier, action_type) != Outcome.DENY

    def propose(action_type: str, params: dict, rationale: str, scope: str, scope_only: bool = False) -> None:
        action = svc.propose(action_type, params, rationale=rationale, origin="cycle", idempotency_scope=scope,
                             scope_only=scope_only)
        if not svc.last_was_new:
            return  # already proposed earlier (idempotent); nothing new to report
        report["proposals"].append({"id": action.id, "type": action_type, "status": action.status,
                                    "summary": action.summary, "reasons": action.decision_reasons})

    # 1. Alerts for anything worth knowing (all tiers, including read-only Observer).
    month = month_key(today)
    for ins in analysis.insights:
        if ins.severity in {"critical", "warning"}:
            if _alert(db, user, today, ins.kind, ins.severity, ins.title, ins.body, f"ins:{ins.id}:{month}"):
                report["alerts"] += 1

    # 2. Budgets for categories the user spends in but has not budgeted (R1).
    if can("set_budget"):
        existing = analysis.budgets
        for s in suggest_budgets(analysis.txns, today)["suggestions"]:
            if s["category"] not in existing and s["suggested_cents"] > 0 and s["category"] != "Other":
                propose("set_budget", {"category": s["category"], "monthly_limit_cents": s["suggested_cents"]},
                        f"Median monthly {s['category']} spend is {fmt(s['basis_cents'])}; {s['note']}",
                        scope=month)

    # 3. Bills due soon or overdue (R3).
    for view in analysis.open_bill_views:
        due = date.fromisoformat(view["due_on"])
        if due > today + timedelta(days=PAY_WINDOW_DAYS):
            continue
        if can("pay_bill"):
            notes = "; ".join(f["title"] for f in view["audit"]["findings"]) or "Bill audit clean."
            propose("pay_bill", {"bill_id": view["id"]},
                    f"{view['payee']} bill of {fmt(view['amount_cents'])} is due {due:%d %b}. {notes}",
                    scope=f"bill:{view['id']}", scope_only=True)
        else:
            if _alert(db, user, today, "bill_due", "warning", f"{view['payee']} bill due {due:%d %b}",
                      f"{fmt(view['amount_cents'])} is due. Your agent tier does not allow paying bills, so please "
                      "pay it yourself.", f"billdue:{view['id']}"):
                report["alerts"] += 1

    # 4. Duplicate charges -> dispute (R3, always human-approved).
    if can("dispute_charge"):
        for f in analysis.findings:
            if f.kind == "duplicate_charge":
                propose("dispute_charge", {"transaction_id": f.txn_ids[1], "reason": "Duplicate charge"},
                        f.detail, scope=f"dispute:{f.txn_ids[1]}", scope_only=True)

    # 5. Price hikes -> suggest cancelling (R3, always human-approved).
    if can("cancel_subscription"):
        for f in analysis.findings:
            if f.kind == "price_hike" and f.merchant:
                propose("cancel_subscription", {"merchant": f.merchant, "reason": "Price increase"},
                        f.detail, scope=f"cancel:{f.merchant}:{f.data.get('new_cents')}")

    # 6. Weekly savings sweep of money the forecast says is safe to move (R2).
    if can("transfer_to_savings") and analysis.goals:
        amount = analysis.safe_to_save_cents
        # Stay inside the autonomous limit so Autopilot can act; larger sweeps would only queue approvals.
        amount = min(amount, policy.per_action_limit_cents)
        amount -= amount % 1000
        goal = next((g for g in analysis.goals if "emergency" in g["name"].lower()), analysis.goals[0])
        if amount >= MIN_SWEEP_CENTS and goal["remaining_cents"] > 0:
            amount = min(amount, goal["remaining_cents"])
            iso = today.isocalendar()
            propose("transfer_to_savings", {"amount_cents": amount, "goal_id": goal["id"]},
                    f"Forecast keeps your balance above the {fmt(policy.min_balance_floor_cents)} floor even on a "
                    f"pessimistic path; moving {fmt(amount)} to {goal['name']}.",
                    scope=f"sweep:{iso[0]}-W{iso[1]}", scope_only=True)

    audit.append(db, user.id, "agent", "agent.cycle_completed", {
        "date": today.isoformat(), "tier": policy.tier, "alerts": report["alerts"],
        "proposals": [{"id": p["id"], "type": p["type"], "status": p["status"]} for p in report["proposals"]],
    })
    return report
