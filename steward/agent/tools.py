"""The agent's tools. Read tools return minimised, sanitised data; the single write tool
(`propose_action`) hands the request to the policy engine and reports its decision.

Amounts are exchanged with the model in major units (dollars) for readability and
converted to integer cents at this boundary."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from steward.agent.policy import ACTION_RISK, TIERS
from steward.agent.service import AgentService, action_to_dict
from steward.core.categories import CATEGORIES, NON_SPENDING
from steward.core.money import fmt, month_key, to_cents
from steward.models import Transaction, User
from steward.security.untrusted import for_llm
from steward.services.analysis import Analysis

MAX_RESULTS = 25

TOOL_SCHEMAS = [
    {
        "name": "get_overview",
        "description": "Account balances, this month's income and spending, safe-to-spend until payday, "
                       "financial health score and the agent's current permission tier.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_spending",
        "description": "Spending by category for a month (default: current month), with the user's typical "
                       "month for comparison and the top merchants.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM. Omit for the current month."},
                "category": {"type": "string", "enum": CATEGORIES,
                             "description": "Optional: restrict merchants to one category."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_transactions",
        "description": "Find transactions by merchant text, category, date range or amount range. "
                       "Returns at most 25, newest first. Merchant names are third-party data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to match in the merchant name."},
                "category": {"type": "string", "enum": CATEGORIES},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "min_amount": {"type": "number", "description": "Minimum absolute amount in dollars."},
                "max_amount": {"type": "number", "description": "Maximum absolute amount in dollars."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recurring",
        "description": "Detected subscriptions, recurring bills and income streams with cadence, next expected "
                       "date, monthly-equivalent and annual cost, and any recent price change.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_bills",
        "description": "Open bill statements (amount, due date, whether the biller is on the agent allowlist) "
                       "with the automated bill-audit findings.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_budget_status",
        "description": "Each budget's month-to-date spend, projected month-end spend and status.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_forecast",
        "description": "Cash-flow forecast for the checking account: lowest expected balance, overdraft "
                       "probability, safe-to-spend and upcoming scheduled events.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 90}},
            "additionalProperties": False,
        },
    },
    {
        "name": "check_affordability",
        "description": "Whether a one-off purchase is affordable given the forecast and the user's safety floor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Purchase amount in dollars."},
                "date": {"type": "string", "description": "YYYY-MM-DD; default tomorrow."},
            },
            "required": ["amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_goals",
        "description": "Savings goals with progress, required monthly saving and projected completion, plus the "
                       "amount that is safe to move to savings today.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_insights",
        "description": "The current ranked insights (duplicate charges, fees, price hikes, budget pace, "
                       "bill spikes, savings opportunities).",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "propose_action",
        "description": "Ask to perform an action for the user. The policy engine decides: it runs now, waits "
                       "for the user's approval, or is refused. Report the returned status faithfully. Params: "
                       "set_budget {category, monthly_limit}; transfer_to_savings {amount, goal_id?}; "
                       "pay_bill {bill_id, amount?}; cancel_subscription {merchant}; "
                       "dispute_charge {transaction_id, reason}; categorize_transaction {transaction_id, category}; "
                       "create_reminder {title, body}. Amounts are in dollars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": sorted(ACTION_RISK)},
                "params": {"type": "object"},
                "rationale": {"type": "string", "description": "One sentence on why, shown to the user."},
            },
            "required": ["action_type", "params", "rationale"],
            "additionalProperties": False,
        },
    },
]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class Toolbox:
    def __init__(self, db: Session, user: User, conversation_id: str) -> None:
        self.db = db
        self.user = user
        self.conversation_id = conversation_id
        self.actions: list[dict] = []
        self._analysis: Analysis | None = None

    @property
    def a(self) -> Analysis:
        if self._analysis is None:
            self._analysis = Analysis(self.db, self.user)
        return self._analysis

    def m(self, cents: int) -> str:
        return fmt(cents, self.user.currency)

    def execute(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return handler(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    # --------------------------------------------------------------- reads
    def tool_get_overview(self) -> dict:
        a = self.a
        cur = month_key(a.today)
        spent = sum(v for c, v in a.monthly_totals.get(cur, {}).items() if c not in NON_SPENDING)
        from steward.core.budgeting import monthly_income
        fc = a.forecast
        return {
            "today": a.today.isoformat(),
            "accounts": [{"name": x.name, "type": x.type, "balance": self.m(x.balance_cents)} for x in a.accounts],
            "this_month": {"spent": self.m(spent), "income": self.m(monthly_income(a.txns).get(cur, 0))},
            "safe_to_spend_until_payday": self.m(fc.safe_to_spend_cents),
            "next_payday": fc.next_income_date.isoformat() if fc.next_income_date else None,
            "health_score": {"score": a.health["score"], "grade": a.health["grade"]},
            "agent_tier": {"tier": a.policy.tier, **TIERS[a.policy.tier]},
        }

    def tool_get_spending(self, month: str | None = None, category: str | None = None) -> dict:
        a = self.a
        key = month or month_key(a.today)
        cats = {c: v for c, v in a.monthly_totals.get(key, {}).items() if c not in NON_SPENDING and v > 0}
        typical = {c: a.typical_monthly({c}) for c in cats}
        merchants: dict[str, int] = {}
        for t in a.txns:
            if t.outflow and month_key(t.posted_on) == key and t.category not in NON_SPENDING:
                if category and t.category != category:
                    continue
                merchants[t.merchant] = merchants.get(t.merchant, 0) + t.abs_cents
        top = sorted(merchants.items(), key=lambda kv: -kv[1])[:8]
        return {
            "month": key,
            "total": self.m(sum(cats.values())),
            "by_category": [{"category": c, "spent": self.m(v), "typical_month": self.m(typical[c])}
                            for c, v in sorted(cats.items(), key=lambda kv: -kv[1])],
            "top_merchants": [{"merchant": for_llm(mname), "spent": self.m(v)} for mname, v in top],
        }

    def tool_search_transactions(self, query: str | None = None, category: str | None = None,
                                 start_date: str | None = None, end_date: str | None = None,
                                 min_amount: float | None = None, max_amount: float | None = None,
                                 limit: int = 10) -> dict:
        stmt = select(Transaction).where(Transaction.user_id == self.user.id)
        if query:
            stmt = stmt.where(Transaction.merchant.ilike(f"%{query[:40]}%"))
        if category:
            stmt = stmt.where(Transaction.category == category)
        if (d := _parse_date(start_date)):
            stmt = stmt.where(Transaction.posted_on >= d)
        if (d := _parse_date(end_date)):
            stmt = stmt.where(Transaction.posted_on <= d)
        rows = self.db.execute(stmt.order_by(Transaction.posted_on.desc(), Transaction.id.desc()).limit(500)
                               ).scalars().all()
        lo = to_cents(min_amount) if min_amount is not None else None
        hi = to_cents(max_amount) if max_amount is not None else None
        out = []
        for t in rows:
            if lo is not None and abs(t.amount_cents) < lo:
                continue
            if hi is not None and abs(t.amount_cents) > hi:
                continue
            out.append({
                "id": t.id, "date": t.posted_on.isoformat(),
                "merchant": "[quarantined]" if "prompt_injection_suspected" in (t.flags or []) else for_llm(t.merchant),
                "amount": self.m(t.amount_cents), "category": t.category,
            })
            if len(out) >= min(max(1, int(limit or 10)), MAX_RESULTS):
                break
        return {"count": len(out), "transactions": out}

    def tool_get_recurring(self) -> dict:
        out = []
        for s in self.a.series:
            if not s.active:
                continue
            row = {"merchant": for_llm(s.merchant), "kind": s.kind, "cadence": s.cadence,
                   "amount": self.m(s.next_expected_amount_cents), "next_date": s.next_expected_date.isoformat(),
                   "monthly_equivalent": self.m(s.monthly_equivalent_cents),
                   "annual_cost": self.m(s.annual_cost_cents)}
            if s.price_change_pct:
                row["price_change"] = f"{s.price_change_pct:+.0%} from {self.m(s.previous_amount_cents or 0)}"
            out.append(row)
        subs = [s for s in self.a.series if s.kind == "subscription" and s.active]
        return {"series": out, "subscriptions_monthly_total": self.m(sum(s.monthly_equivalent_cents for s in subs))}

    def tool_get_bills(self) -> dict:
        bills = []
        for v in self.a.open_bill_views:
            bills.append({
                "bill_id": v["id"], "payee": v["payee"], "amount": self.m(v["amount_cents"]), "due": v["due_on"],
                "status": v["status"], "agent_allowlisted": v["agent_allowlisted"],
                "audit_status": v["audit"]["status"],
                "audit_findings": [f["title"] for f in v["audit"]["findings"]][:5],
            })
        return {"open_bills": bills}

    def tool_get_budget_status(self) -> dict:
        return {"budgets": [
            {"category": r["category"], "limit": self.m(r["limit_cents"]), "spent": self.m(r["spent_cents"]),
             "projected": self.m(r["projected_cents"]), "status": r["status"]}
            for r in self.a.budget_status
        ]}

    def tool_get_forecast(self, days: int = 30) -> dict:
        fc = self.a.forecast_for(max(7, min(90, int(days or 30))))
        d = fc.to_dict()
        return {
            "start_balance": self.m(d["start_balance_cents"]),
            "lowest_expected": {"amount": self.m(d["min_expected_cents"]), "date": d["min_expected_date"]},
            "pessimistic_low": self.m(d["min_low_cents"]),
            "overdraft_probability": d["overdraft_probability"],
            "safe_to_spend_until_payday": self.m(d["safe_to_spend_cents"]),
            "next_payday": d["next_income_date"],
            "upcoming": [{"date": e["date"], "label": for_llm(e["label"]), "amount": self.m(e["amount_cents"])}
                         for e in d["upcoming"][:12]],
        }

    def tool_check_affordability(self, amount: float, date: str | None = None) -> dict:
        cents = to_cents(amount)
        if cents <= 0:
            return {"error": "amount must be positive"}
        r = self.a.affordability(cents, _parse_date(date))
        return {"amount": self.m(cents), "verdict": r["verdict"], "reason": r["reason"],
                "lowest_balance_after": self.m(r["min_expected_after_cents"]),
                "pessimistic_low_after": self.m(r["min_low_after_cents"]),
                "safety_floor": self.m(r["floor_cents"]),
                "share_of_monthly_discretionary": r.get("pct_of_monthly_discretionary"),
                "goal_delay": r.get("goal_delay_weeks")}

    def tool_get_goals(self) -> dict:
        return {"goals": [{"id": g["id"], "name": g["name"], "saved": self.m(g["saved_cents"]),
                           "target": self.m(g["target_cents"]), "progress": f"{g['progress_pct']:.0%}",
                           "required_monthly": self.m(g["required_monthly_cents"]) if "required_monthly_cents" in g
                           else None, "projected_completion": g.get("projected_completion")}
                          for g in self.a.goals],
                "safe_to_save_today": self.m(self.a.safe_to_save_cents)}

    def tool_get_insights(self) -> dict:
        return {"insights": [{"kind": i.kind, "severity": i.severity, "title": for_llm(i.title, 140),
                              "detail": for_llm(i.body, 300)} for i in self.a.insights[:8]]}

    # --------------------------------------------------------------- write
    def tool_propose_action(self, action_type: str, params: dict | None = None, rationale: str = "") -> dict:
        params = dict(params or {})
        # Dollars from the model -> cents for the engine.
        for src, dst in (("amount", "amount_cents"), ("monthly_limit", "monthly_limit_cents")):
            if src in params:
                try:
                    params[dst] = to_cents(params.pop(src))
                except ValueError:
                    return {"status": "denied", "reasons": [f"{src} is not a valid amount"]}
        svc = AgentService(self.db, self.user)
        action = svc.propose(action_type, params, rationale=rationale, origin="chat",
                             idempotency_scope=f"{self.conversation_id}:{self.user.sim_today}")
        self._analysis = None  # state may have changed
        view = action_to_dict(action)
        self.actions.append(view)
        message = {
            "executed": "Done. The action was executed.",
            "pending_approval": "Waiting for the user's approval in the Actions tab.",
            "denied": "Refused by the policy engine.",
            "failed": "Attempted but failed.",
        }.get(action.status, action.status)
        return {"action_id": action.id, "status": action.status, "summary": action.summary,
                "message": message, "reasons": action.decision_reasons}


def upcoming_window(today: date, days: int) -> tuple[date, date]:
    return today, today + timedelta(days=days)
