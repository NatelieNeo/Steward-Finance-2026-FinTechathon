"""Loads a user's data once and runs the analytics engines over it (lazily, cached per request)."""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from functools import cached_property

from sqlalchemy import select
from sqlalchemy.orm import Session

from steward.core import anomalies, budgeting, health
from steward.core.bill_audit import audit_bill
from steward.core.categories import NEEDS, NON_SPENDING, WANTS
from steward.core.forecast import Forecast, build_forecast, check_affordability
from steward.core.goals import goal_progress, safe_to_save
from steward.core.insights import Insight, build_insights
from steward.core.money import add_months, fmt, month_key, previous_months
from steward.core.recurring import RecurringSeries, detect_recurring, project_occurrences
from steward.core.types import Txn
from steward.models import Account, AgentPolicy, Bill, Budget, Goal, Payee, SubscriptionState, Transaction, User


def today_for(user: User) -> date:
    return user.sim_today or date.today()


class Analysis:
    def __init__(self, db: Session, user: User, lookback_days: int = 400) -> None:
        self.db = db
        self.user = user
        self.today = today_for(user)
        self.currency = user.currency
        since = self.today - timedelta(days=lookback_days)
        rows = db.execute(
            select(Transaction).where(Transaction.user_id == user.id, Transaction.posted_on >= since,
                                      Transaction.posted_on <= self.today)
            .order_by(Transaction.posted_on, Transaction.id)
        ).scalars().all()
        self.txns = [
            Txn(id=t.id, posted_on=t.posted_on, description=t.description, merchant=t.merchant,
                amount_cents=t.amount_cents, category=t.category, account_id=t.account_id, kind=t.kind,
                flags=tuple(t.flags or ()))
            for t in rows
        ]
        self.accounts = db.execute(select(Account).where(Account.user_id == user.id)).scalars().all()
        self.policy = db.get(AgentPolicy, user.id) or AgentPolicy(user_id=user.id, tier=1,
                                                                  min_balance_floor_cents=100_000)

    # ------------------------------------------------------------------ basics
    @property
    def checking(self) -> Account | None:
        return next((a for a in self.accounts if a.type == "checking"), None)

    @property
    def savings(self) -> Account | None:
        return next((a for a in self.accounts if a.type == "savings"), None)

    @cached_property
    def payees(self) -> dict[int, Payee]:
        return {p.id: p for p in self.db.execute(select(Payee).where(Payee.user_id == self.user.id)).scalars()}

    @cached_property
    def bills(self) -> list[Bill]:
        return self.db.execute(
            select(Bill).where(Bill.user_id == self.user.id).order_by(Bill.issued_on.desc())
        ).scalars().all()

    @cached_property
    def open_bills(self) -> list[Bill]:
        return [b for b in self.bills if b.status in {"open", "overdue"} and b.issued_on <= self.today]

    @cached_property
    def cancelled_merchants(self) -> set[str]:
        return set(self.db.execute(
            select(SubscriptionState.merchant).where(SubscriptionState.user_id == self.user.id,
                                                     SubscriptionState.status == "cancelled")
        ).scalars())

    # ---------------------------------------------------------------- engines
    @cached_property
    def series(self) -> list[RecurringSeries]:
        out = detect_recurring(self.txns, self.today)
        for s in out:
            if s.merchant in self.cancelled_merchants:
                s.active = False
        return out

    @cached_property
    def forecast(self) -> Forecast:
        return self.forecast_for(30)

    def forecast_for(self, days: int) -> Forecast:
        checking = self.checking
        open_bills = [
            {"due_on": b.due_on, "amount_cents": b.amount_cents, "label": f"{self.payees[b.payee_id].name} bill",
             "merchant": self.payees[b.payee_id].name}
            for b in self.open_bills
        ]
        return build_forecast(
            start_balance_cents=checking.balance_cents if checking else 0,
            today=self.today,
            txns=self.txns,
            series=self.series,
            open_bills=open_bills,
            horizon_days=days,
            floor_cents=self.policy.min_balance_floor_cents,
            account_ids={checking.id} if checking else None,
        )

    def affordability(self, amount_cents: int, on: date | None = None) -> dict:
        result = check_affordability(self.forecast_for(45), amount_cents, on)
        monthly_wants = self.typical_monthly(WANTS)
        if monthly_wants:
            result["pct_of_monthly_discretionary"] = round(amount_cents / monthly_wants, 3)
        goals = self.goals
        if goals:
            g = goals[0]
            contrib = max(g["avg_monthly_contribution_cents"], 1)
            result["goal_delay_weeks"] = {"goal": g["name"], "weeks": round(amount_cents / contrib * 4.345, 1)}
        return result

    @cached_property
    def monthly_totals(self) -> dict[str, dict[str, int]]:
        return budgeting.monthly_category_totals(self.txns)

    def typical_monthly(self, cats: set[str], months: int = 3) -> int:
        keys = previous_months(self.today, months)
        vals = [sum(v for c, v in self.monthly_totals.get(k, {}).items() if c in cats) for k in keys]
        return int(statistics.median(vals)) if vals else 0

    @cached_property
    def monthly_income(self) -> int:
        return budgeting.typical_monthly_income(self.txns, self.today)

    @cached_property
    def budgets(self) -> dict[str, int]:
        rows = self.db.execute(select(Budget).where(Budget.user_id == self.user.id)).scalars()
        return {b.category: b.monthly_limit_cents for b in rows}

    @cached_property
    def scheduled_remaining(self) -> dict[str, int]:
        """Recurring charges and open bills still expected this month, by category."""
        end = add_months(self.today.replace(day=1), 1) - timedelta(days=1)
        out: dict[str, int] = {}
        for s in self.series:
            if s.direction != "outflow" or not s.active:
                continue
            n = len(project_occurrences(s, self.today, end))
            if n:
                out[s.category] = out.get(s.category, 0) + n * s.next_expected_amount_cents
        for b in self.open_bills:
            if b.due_on <= end:
                cat = self.payees[b.payee_id].category
                out[cat] = out.get(cat, 0) + b.amount_cents
        return out

    @cached_property
    def budget_status(self) -> list[dict]:
        recurring = {s.merchant for s in self.series if s.direction == "outflow"}
        return budgeting.budget_status(self.budgets, self.txns, self.today, self.scheduled_remaining, recurring)

    @cached_property
    def goals(self) -> list[dict]:
        goals = self.db.execute(select(Goal).where(Goal.user_id == self.user.id).order_by(Goal.id)).scalars().all()
        # Contributions = transfers into savings over the last 3 full months, split evenly across goals.
        savings_ids = {a.id for a in self.accounts if a.type == "savings"}
        keys = set(previous_months(self.today, 3))
        inflow = sum(t.amount_cents for t in self.txns
                     if t.account_id in savings_ids and t.kind == "transfer" and t.amount_cents > 0
                     and month_key(t.posted_on) in keys)
        per_goal = int(inflow / 3 / max(1, len(goals)))
        out = []
        for g in goals:
            row = goal_progress(g.name, g.target_cents, g.saved_cents, g.target_date, self.today, per_goal)
            row["id"] = g.id
            out.append(row)
        return out

    @cached_property
    def safe_to_save_cents(self) -> int:
        return safe_to_save(self.forecast)

    @cached_property
    def findings(self):
        return (
            anomalies.find_duplicate_charges(self.txns, self.today)
            + anomalies.find_fees(self.txns, self.today)
            + anomalies.find_outliers(self.txns, self.today)
            + anomalies.find_bill_spikes(self.series)
            + anomalies.find_price_hikes(
                [s for s in self.series if s.merchant not in self.cancelled_merchants], self.today)
            + anomalies.find_subscription_overlaps(
                [s for s in self.series if s.merchant not in self.cancelled_merchants])
            + self._bill_fee_findings()
        )

    def _bill_fee_findings(self, days: int = 120):
        """Late/admin charges hidden inside bill statements (they never show up as their own transaction)."""
        from steward.core.types import Finding

        since = self.today - timedelta(days=days)
        hits = [(b, i) for b in self.bills if b.issued_on >= since for i in (b.line_items or [])
                if i.get("type") == "fee"]
        if not hits:
            return []
        total = sum(i["amount_cents"] for _, i in hits)
        where = ", ".join(sorted({f"{self.payees[b.payee_id].name} ({b.period_label})" for b, _ in hits}))
        return [Finding(
            kind="bill_fees", severity="warning",
            title=f"{fmt(total, self.currency)} in late charges on your bills",
            detail=f"Charged on: {where}. Paying before the due date (or letting Steward pay allowlisted bills "
                   "on Autopilot) avoids these.",
            amount_cents=total, data={"bills": [b.id for b, _ in hits]},
        )]

    def bill_view(self, b: Bill) -> dict:
        payee = self.payees[b.payee_id]
        history = [x for x in self.bills if x.payee_id == b.payee_id and x.issued_on < b.issued_on]
        previous = history[0] if history else None
        audit_result = audit_bill(
            b.line_items,
            stated_total_cents=b.amount_cents,
            previous_items=previous.line_items if previous else None,
            history_totals_cents=[x.amount_cents for x in history[:6]],
            payee=payee.name,
        )
        return {
            "id": b.id,
            "payee": payee.name,
            "payee_id": payee.id,
            "agent_allowlisted": payee.agent_allowlisted,
            "period": b.period_label,
            "issued_on": b.issued_on.isoformat(),
            "due_on": b.due_on.isoformat(),
            "amount_cents": b.amount_cents,
            "status": b.status,
            "paid_on": b.paid_on.isoformat() if b.paid_on else None,
            "paid_by": b.paid_by,
            "days_until_due": (b.due_on - self.today).days,
            "audit": audit_result,
        }

    @cached_property
    def open_bill_views(self) -> list[dict]:
        return sorted((self.bill_view(b) for b in self.open_bills), key=lambda v: v["due_on"])

    def month_summary(self, key: str) -> dict:
        spend = sum(v for c, v in self.monthly_totals.get(key, {}).items() if c not in NON_SPENDING)
        income = budgeting.monthly_income(self.txns).get(key, 0)
        return {"month": key, "income_cents": income, "spend_cents": spend,
                "savings_rate": round((income - spend) / income, 4) if income else 0.0}

    @cached_property
    def mom_changes(self) -> list[dict]:
        """Category spend month-to-date vs the same days last month."""
        cur_start = self.today.replace(day=1)
        prev_start = add_months(cur_start, -1)
        prev_end = add_months(self.today, -1)
        cur: dict[str, int] = {}
        prev: dict[str, int] = {}
        for t in self.txns:
            if not t.outflow or t.category in NON_SPENDING:
                continue
            if cur_start <= t.posted_on <= self.today:
                cur[t.category] = cur.get(t.category, 0) + t.abs_cents
            elif prev_start <= t.posted_on <= prev_end:
                prev[t.category] = prev.get(t.category, 0) + t.abs_cents
        rows = []
        for cat in set(cur) | set(prev):
            c, p = cur.get(cat, 0), prev.get(cat, 0)
            rows.append({"category": cat, "current_cents": c, "previous_cents": p, "delta_cents": c - p,
                         "pct": round((c - p) / p, 4) if p else None})
        rows.sort(key=lambda r: -r["delta_cents"])
        return [r for r in rows if r["pct"] is not None]

    @cached_property
    def quarantined(self) -> list[dict]:
        return [{"id": t.id, "date": t.posted_on.isoformat()} for t in self.txns
                if "prompt_injection_suspected" in t.flags]

    @cached_property
    def insights(self) -> list[Insight]:
        goal = None
        if self.goals:
            g = min(self.goals, key=lambda x: x["progress_pct"])
            ef = next((x for x in self.goals if "emergency" in x["name"].lower()), g)
            goal = {"id": ef["id"], "name": ef["name"]}
        last_month = previous_months(self.today, 1)[0]
        return build_insights(
            today=self.today,
            currency=self.currency,
            findings=self.findings,
            budget_rows=self.budget_status,
            forecast=self.forecast.to_dict(),
            safe_to_save_cents=self.safe_to_save_cents,
            savings_goal=goal,
            open_bills=self.open_bill_views,
            month_summary=self.month_summary(last_month),
            mom_changes=self.mom_changes,
            quarantined=self.quarantined,
        )

    @cached_property
    def health(self) -> dict:
        keys = previous_months(self.today, 3)
        spend = [sum(v for c, v in self.monthly_totals.get(k, {}).items() if c not in NON_SPENDING) for k in keys]
        wants = [sum(v for c, v in self.monthly_totals.get(k, {}).items() if c in WANTS) for k in keys]
        fees = [f for f in self.findings if f.kind == "fees"]
        bill_fees = [f for f in self.findings if f.kind == "bill_fees"]
        rows = self.budget_status
        return health.score(health.HealthInputs(
            monthly_income_cents=self.monthly_income,
            monthly_spend_cents=spend,
            monthly_essential_cents=self.typical_monthly(NEEDS),
            liquid_savings_cents=sum(a.balance_cents for a in self.accounts if a.type == "savings"),
            fees_last_90d_cents=sum(f.amount_cents for f in fees + bill_fees),
            fee_count_last_90d=(fees[0].data["count"] if fees else 0)
            + (len(bill_fees[0].data["bills"]) if bill_fees else 0),
            budgets_total=len(rows),
            budgets_on_track=sum(1 for r in rows if r["status"] == "on_track"),
            debt_cents=sum(-a.balance_cents for a in self.accounts if a.type == "credit" and a.balance_cents < 0),
            discretionary_monthly_cents=wants,
        ))

    def spending_summary(self, months: int = 6) -> dict:
        keys = previous_months(self.today, months - 1) + [month_key(self.today)]
        by_month = []
        for k in keys:
            cats = {c: v for c, v in self.monthly_totals.get(k, {}).items() if c not in NON_SPENDING and v > 0}
            by_month.append({"month": k, "total_cents": sum(cats.values()), "categories": cats,
                             "income_cents": budgeting.monthly_income(self.txns).get(k, 0)})
        cur = month_key(self.today)
        merchants: dict[str, int] = {}
        for t in self.txns:
            if t.outflow and t.category not in NON_SPENDING and month_key(t.posted_on) == cur:
                merchants[t.merchant] = merchants.get(t.merchant, 0) + t.abs_cents
        top = sorted(merchants.items(), key=lambda kv: -kv[1])[:10]
        return {"months": by_month, "top_merchants_this_month": [{"merchant": m, "spent_cents": v} for m, v in top],
                "mom_changes": self.mom_changes[:6]}
