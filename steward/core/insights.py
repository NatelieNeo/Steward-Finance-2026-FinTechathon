"""Turn engine outputs into ranked, plain-language insights with optional one-click actions.

Every number in an insight comes from a deterministic engine. The optional LLM digest only
rephrases these facts, and its numbers are checked against them (see agent.digest)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from steward.core.money import fmt
from steward.core.types import Finding

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2, "positive": 3}


@dataclass
class Insight:
    kind: str
    severity: str
    title: str
    body: str
    amount_cents: int = 0
    suggested_action: dict | None = None
    evidence: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            key = f"{self.kind}|{self.title}"
            self.id = hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return asdict(self)


def from_finding(f: Finding) -> Insight:
    action = None
    if f.kind == "duplicate_charge" and len(f.txn_ids) == 2:
        action = {
            "action_type": "dispute_charge",
            "params": {"transaction_id": f.txn_ids[1], "reason": "Duplicate charge"},
            "label": "Dispute the second charge",
        }
    elif f.kind == "price_hike" and f.merchant:
        action = {
            "action_type": "cancel_subscription",
            "params": {"merchant": f.merchant, "reason": "Price increase"},
            "label": f"Cancel {f.merchant}",
        }
    elif f.kind == "subscription_overlap":
        merchants = f.data.get("merchants", [])
        if merchants:
            action = {
                "action_type": "cancel_subscription",
                "params": {"merchant": merchants[-1], "reason": "Overlapping subscription"},
                "label": f"Cancel {merchants[-1]}",
            }
    return Insight(
        kind=f.kind,
        severity=f.severity,
        title=f.title,
        body=f.detail,
        amount_cents=f.amount_cents,
        suggested_action=action,
        evidence={"txn_ids": f.txn_ids, "merchant": f.merchant,
                  "date": f.on.isoformat() if f.on else None, **f.data},
    )


def build_insights(
    *,
    today: date,
    currency: str,
    findings: list[Finding],
    budget_rows: list[dict],
    forecast: dict,
    safe_to_save_cents: int,
    savings_goal: dict | None,
    open_bills: list[dict],
    month_summary: dict,
    mom_changes: list[dict],
    quarantined: list[dict],
) -> list[Insight]:
    out: list[Insight] = [from_finding(f) for f in findings]

    for row in budget_rows:
        if row["status"] in {"over", "at_risk"}:
            days_left = round((1 - row["month_elapsed_pct"]) * 30)
            if row["status"] == "over":
                title = f"{row['category']} budget is already over"
                body = (f"Spent {fmt(row['spent_cents'], currency)} of {fmt(row['limit_cents'], currency)} "
                        f"with about {days_left} days left in the month.")
            else:
                title = f"{row['category']} is on pace to overspend"
                body = (f"{row['pct_used']:.0%} of the {fmt(row['limit_cents'], currency)} budget is used "
                        f"{row['month_elapsed_pct']:.0%} of the way through the month. Projected month-end: "
                        f"{fmt(row['projected_cents'], currency)} ({fmt(row['projected_over_cents'], currency)} over).")
            out.append(Insight("budget_pace", "warning", title, body, row["projected_over_cents"],
                               evidence={"category": row["category"]}))

    p_over = forecast.get("overdraft_probability", 0)
    if p_over >= 0.05:
        out.append(Insight(
            "low_balance", "critical" if p_over >= 0.25 else "warning",
            "Your balance may run low before payday",
            f"Lowest expected balance is {fmt(forecast['min_expected_cents'], currency)} on "
            f"{date.fromisoformat(forecast['min_expected_date']):%d %b}, with a {p_over:.0%} chance of going below zero.",
            forecast["min_expected_cents"],
            evidence={"min_expected_date": forecast["min_expected_date"]},
        ))

    if safe_to_save_cents >= 5000 and savings_goal:
        out.append(Insight(
            "savings_opportunity", "positive",
            f"You can safely move {fmt(safe_to_save_cents, currency)} to {savings_goal['name']}",
            f"Even on a pessimistic path your balance stays above your {fmt(forecast['floor_cents'], currency)} "
            f"buffer for the next {forecast['horizon_days']} days.",
            safe_to_save_cents,
            suggested_action={
                "action_type": "transfer_to_savings",
                "params": {"amount_cents": safe_to_save_cents, "goal_id": savings_goal["id"]},
                "label": f"Move {fmt(safe_to_save_cents, currency)} to savings",
            },
        ))

    due_soon = [b for b in open_bills if date.fromisoformat(b["due_on"]) <= today + timedelta(days=7)]
    if due_soon:
        total = sum(b["amount_cents"] for b in due_soon)
        names = ", ".join(f"{b['payee']} ({date.fromisoformat(b['due_on']):%d %b})" for b in due_soon)
        out.append(Insight(
            "upcoming_bills", "info", f"{len(due_soon)} bill(s) due in the next 7 days",
            f"{names}: {fmt(total, currency)} in total.", total,
            evidence={"bill_ids": [b["id"] for b in due_soon]},
        ))

    for ch in mom_changes[:1]:
        if ch["delta_cents"] > 5000 and ch["pct"] >= 0.25:
            out.append(Insight(
                "mom_change", "info", f"{ch['category']} spending up {ch['pct']:.0%} on last month",
                f"{fmt(ch['current_cents'], currency)} so far this month vs {fmt(ch['previous_cents'], currency)} "
                "by the same day last month.", ch["delta_cents"], evidence=ch,
            ))

    if quarantined:
        out.append(Insight(
            "prompt_injection", "warning", "A transaction description tried to instruct the AI",
            f"{len(quarantined)} transaction(s) contained text aimed at AI assistants (e.g. 'ignore previous "
            "instructions'). Steward quarantined the text: it is never shown to the AI and cannot trigger actions.",
            evidence={"txn_ids": [q["id"] for q in quarantined]},
        ))

    if month_summary.get("income_cents"):
        rate = month_summary["savings_rate"]
        title = (f"Last month you kept {rate:.0%} of your income" if rate >= 0
                 else f"Last month you spent {-rate:.0%} more than you earned")
        out.append(Insight(
            "month_summary", "positive" if rate >= 0.2 else "info" if rate >= 0 else "warning",
            title,
            f"Income {fmt(month_summary['income_cents'], currency)}, spending "
            f"{fmt(month_summary['spend_cents'], currency)}.",
            month_summary["income_cents"] - month_summary["spend_cents"],
            evidence=month_summary,
        ))

    out.sort(key=lambda i: (SEVERITY_RANK.get(i.severity, 9), -abs(i.amount_cents)))
    return out
