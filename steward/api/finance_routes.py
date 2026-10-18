"""Read and manage money: overview, transactions, imports, insights, bills, budgets, goals."""

from __future__ import annotations

import csv
import datetime as dt
import io
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from steward.agent.digest import weekly_digest
from steward.agent.policy import TIERS
from steward.api.deps import Principal, current, get_db, rate_limit, require_step_up
from steward.core import importer
from steward.core.bill_audit import audit_bill, parse_bill_text
from steward.core.budgeting import monthly_income, suggest_budgets
from steward.core.categories import BUDGETABLE, CATEGORIES, NON_SPENDING
from steward.core.money import month_key
from steward.models import Account, Alert, Bill, Budget, Goal, MerchantMemory, Payee, SubscriptionState, Transaction
from steward.sandbox import bank
from steward.security import audit
from steward.services.analysis import Analysis

router = APIRouter(tags=["finance"])


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def txn_view(t: Transaction) -> dict:
    return {"id": t.id, "account_id": t.account_id, "date": t.posted_on.isoformat(), "description": t.description,
            "merchant": t.merchant, "amount_cents": t.amount_cents, "category": t.category,
            "category_source": t.category_source, "confidence": round(t.category_confidence, 3), "kind": t.kind,
            "flags": t.flags or [], "created_by": t.created_by}


# ------------------------------------------------------------------- overview
@router.get("/overview")
def overview(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    a = Analysis(db, p.user)
    cur = month_key(a.today)
    spent = sum(v for c, v in a.monthly_totals.get(cur, {}).items() if c not in NON_SPENDING)
    fc = a.forecast
    alerts = db.execute(select(Alert).where(Alert.user_id == p.user.id, Alert.read.is_(False))
                        .order_by(Alert.id.desc()).limit(8)).scalars().all()
    return {
        "today": a.today.isoformat(),
        "accounts": [{"id": x.id, "name": x.name, "type": x.type, "mask": x.mask, "balance_cents": x.balance_cents,
                      "institution": x.institution} for x in a.accounts],
        "month": {"key": cur, "spent_cents": spent, "income_cents": monthly_income(a.txns).get(cur, 0)},
        "safe_to_spend_cents": fc.safe_to_spend_cents,
        "next_payday": fc.next_income_date.isoformat() if fc.next_income_date else None,
        "forecast": fc.to_dict(),
        "health": a.health,
        "insights": [i.to_dict() for i in a.insights[:6]],
        "open_bills": a.open_bill_views,
        "budgets": a.budget_status,
        "agent": {"tier": a.policy.tier, "frozen": a.policy.frozen, **TIERS[a.policy.tier]},
        "alerts": [{"id": x.id, "kind": x.kind, "severity": x.severity, "title": x.title, "body": x.body,
                    "date": x.created_on.isoformat()} for x in alerts],
    }


@router.get("/accounts")
def accounts(p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Account).where(Account.user_id == p.user.id).order_by(Account.id)).scalars()
    return [{"id": a.id, "name": a.name, "type": a.type, "mask": a.mask, "balance_cents": a.balance_cents,
             "currency": a.currency, "institution": a.institution} for a in rows]


# --------------------------------------------------------------- transactions
@router.get("/transactions")
def transactions(
    p: Principal = Depends(current), db: Session = Depends(get_db),
    q: str | None = Query(None, max_length=80), category: str | None = Query(None, max_length=40),
    start: date | None = None, end: date | None = None, account_id: int | None = None,
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
) -> dict:
    stmt = select(Transaction).where(Transaction.user_id == p.user.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Transaction.merchant.ilike(like) | Transaction.description.ilike(like))
    if category:
        stmt = stmt.where(Transaction.category == category)
    if start:
        stmt = stmt.where(Transaction.posted_on >= start)
    if end:
        stmt = stmt.where(Transaction.posted_on <= end)
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Transaction.posted_on.desc(), Transaction.id.desc()).limit(limit).offset(offset)
                      ).scalars().all()
    return {"total": total, "items": [txn_view(t) for t in rows], "categories": CATEGORIES}


class RecategorizeIn(BaseModel):
    category: str = Field(max_length=40)
    remember: bool = True


@router.patch("/transactions/{txn_id}")
def recategorize(txn_id: int, body: RecategorizeIn, p: Principal = Depends(current),
                 db: Session = Depends(get_db)) -> dict:
    if body.category not in CATEGORIES:
        raise _err(422, "bad_category", "Unknown category.")
    t = db.get(Transaction, txn_id)
    if t is None or t.user_id != p.user.id:
        raise _err(404, "not_found", "Transaction not found.")
    old = t.category
    t.category, t.category_source, t.category_confidence = body.category, "user", 1.0
    updated = 1
    if body.remember and "prompt_injection_suspected" not in (t.flags or []):
        mem = db.execute(select(MerchantMemory).where(MerchantMemory.user_id == p.user.id,
                                                      MerchantMemory.merchant == t.merchant)).scalar_one_or_none()
        if mem is None:
            db.add(MerchantMemory(user_id=p.user.id, merchant=t.merchant, category=body.category))
        else:
            mem.category, mem.updated_at = body.category, datetime.now(timezone.utc)
        # Apply the lesson to the same merchant's other auto-categorised transactions.
        others = db.execute(select(Transaction).where(
            Transaction.user_id == p.user.id, Transaction.merchant == t.merchant, Transaction.id != t.id,
            Transaction.category_source != "user")).scalars().all()
        for o in others:
            o.category, o.category_source, o.category_confidence = body.category, "memory", 0.99
        updated += len(others)
    audit.append(db, p.user.id, "user", "transaction.recategorized",
                 {"txn_id": t.id, "merchant": t.merchant, "from": old, "to": body.category, "applied_to": updated})
    db.commit()
    return {"updated": updated, "transaction": txn_view(t)}


@router.post("/transactions/import")
async def import_csv(request: Request, file: UploadFile = File(...), account_id: int = Form(...),
                     p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"import:{p.user.id}", 10, 3600)
    account = db.get(Account, account_id)
    if account is None or account.user_id != p.user.id:
        raise _err(404, "not_found", "Account not found.")
    content = await file.read(importer.MAX_BYTES + 1)
    try:
        rows, warnings = importer.parse_csv(content)
    except importer.ImportError_ as exc:
        raise _err(422, "bad_file", str(exc)) from exc
    memory = dict(db.execute(select(MerchantMemory.merchant, MerchantMemory.category)
                             .where(MerchantMemory.user_id == p.user.id)).all())
    seen: dict[tuple, int] = {}
    imported = duplicates = quarantined = net = 0
    for row in rows:
        k = (row.posted_on, row.amount_cents, row.description.upper())
        seen[k] = seen.get(k, 0) + 1
        h = importer.import_hash(p.user.id, account.id, row, seen[k])
        if db.execute(select(Transaction.id).where(Transaction.user_id == p.user.id,
                                                   Transaction.import_hash == h)).first():
            duplicates += 1
            continue
        txn = bank.post(db, account, on=row.posted_on, description=row.description, amount_cents=row.amount_cents,
                        kind="purchase" if row.amount_cents < 0 else "income", created_by="import",
                        allow_overdraft=True, import_hash=h, memory=memory)
        imported += 1
        net += row.amount_cents
        quarantined += "prompt_injection_suspected" in txn.flags
    # A statement import back-fills history that is already reflected in today's balance, so
    # the balance stays put and the opening balance absorbs the history (ledger invariant holds).
    account.balance_cents -= net
    account.opening_balance_cents -= net
    audit.append(db, p.user.id, "user", "transactions.imported",
                 {"account_id": account.id, "imported": imported, "duplicates": duplicates,
                  "quarantined": quarantined, "warnings": len(warnings)})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _err(409, "duplicate", "This file overlaps an import in progress; try again.") from exc
    return {"imported": imported, "duplicates_skipped": duplicates, "quarantined": quarantined,
            "warnings": warnings[:20]}


@router.get("/transactions/export.csv")
def export_csv(p: Principal = Depends(current), db: Session = Depends(get_db)) -> StreamingResponse:
    rows = db.execute(select(Transaction).where(Transaction.user_id == p.user.id)
                      .order_by(Transaction.posted_on, Transaction.id)).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "description", "merchant", "category", "amount"])
    for t in rows:
        w.writerow([t.posted_on.isoformat(), importer.csv_safe(t.description), importer.csv_safe(t.merchant),
                    t.category, f"{t.amount_cents / 100:.2f}"])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="steward-transactions.csv"'})


# ------------------------------------------------------------------ analysis
@router.get("/spending")
def spending(months: int = Query(6, ge=2, le=12), p: Principal = Depends(current),
             db: Session = Depends(get_db)) -> dict:
    return Analysis(db, p.user).spending_summary(months)


@router.get("/insights")
def insights(p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    return [i.to_dict() for i in Analysis(db, p.user).insights]


@router.get("/digest")
def digest(request: Request, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"digest:{p.user.id}", 10, 60)
    return weekly_digest(Analysis(db, p.user), request.app.state.llm)


@router.get("/recurring")
def recurring(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    a = Analysis(db, p.user)
    states = {s.merchant: s.status for s in db.execute(
        select(SubscriptionState).where(SubscriptionState.user_id == p.user.id)).scalars()}
    rows = []
    for s in a.series:
        d = s.to_dict()
        d["user_status"] = states.get(s.merchant, "active" if s.active else "lapsed")
        rows.append(d)
    subs = [s for s in a.series if s.kind == "subscription" and s.active]
    return {"series": rows, "subscriptions_monthly_cents": sum(s.monthly_equivalent_cents for s in subs),
            "subscriptions_annual_cents": sum(s.annual_cost_cents for s in subs)}


@router.get("/forecast")
def forecast(days: int = Query(30, ge=7, le=90), p: Principal = Depends(current),
             db: Session = Depends(get_db)) -> dict:
    return Analysis(db, p.user).forecast_for(days).to_dict()


class AffordIn(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    date: dt.date | None = None


@router.post("/affordability")
def affordability(body: AffordIn, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    return Analysis(db, p.user).affordability(body.amount_cents, body.date)


@router.get("/health")
def health_score(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    return Analysis(db, p.user).health


# ---------------------------------------------------------------------- bills
@router.get("/bills")
def bills(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    a = Analysis(db, p.user)
    return {"open": a.open_bill_views,
            "history": [a.bill_view(b) for b in a.bills if b.status == "paid"][:24]}


@router.get("/bills/{bill_id}")
def bill_detail(bill_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    b = db.get(Bill, bill_id)
    if b is None or b.user_id != p.user.id:
        raise _err(404, "not_found", "Bill not found.")
    return Analysis(db, p.user).bill_view(b)


@router.post("/bills/{bill_id}/pay")
def pay_bill_myself(bill_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    """The user paying their own bill directly (not delegated to the agent)."""
    b = db.get(Bill, bill_id)
    if b is None or b.user_id != p.user.id:
        raise _err(404, "not_found", "Bill not found.")
    if b.status not in {"open", "overdue"}:
        raise _err(409, "not_payable", f"Bill is already {b.status}.")
    checking = db.execute(select(Account).where(Account.user_id == p.user.id, Account.type == "checking")
                          ).scalars().first()
    payee = db.get(Payee, b.payee_id)
    today = Analysis(db, p.user).today
    try:
        txn = bank.pay_biller(db, checking, payee, b.amount_cents, today, b.period_label, created_by="user")
    except bank.InsufficientFunds as exc:
        raise _err(409, "insufficient_funds", str(exc)) from exc
    b.status, b.paid_on, b.paid_by = "paid", today, "user"
    audit.append(db, p.user.id, "user", "bill.paid_by_user", {"bill_id": b.id, "amount_cents": b.amount_cents,
                                                               "txn_id": txn.id})
    db.commit()
    return {"ok": True, "txn_id": txn.id}


class AnalyzeBillIn(BaseModel):
    text: str = Field(min_length=5, max_length=20_000)
    payee: str | None = Field(None, max_length=80)


@router.post("/bills/analyze")
def analyze_bill(body: AnalyzeBillIn, request: Request, p: Principal = Depends(current),
                 db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"billscan:{p.user.id}", 30, 60)
    parsed = parse_bill_text(body.text)
    if not parsed["line_items"]:
        raise _err(422, "no_lines", "No line items with amounts were found. Paste lines like 'Plan fee  45.90'.")
    previous, history = None, None
    if body.payee:
        payee = db.execute(select(Payee).where(Payee.user_id == p.user.id, Payee.name == body.payee)).scalar_one_or_none()
        if payee:
            past = db.execute(select(Bill).where(Bill.user_id == p.user.id, Bill.payee_id == payee.id)
                              .order_by(Bill.issued_on.desc())).scalars().all()
            previous = past[0].line_items if past else None
            history = [b.amount_cents for b in past[:6]]
    return audit_bill(parsed["line_items"], parsed["stated_total_cents"], previous, history, body.payee or "this biller")


# -------------------------------------------------------------------- budgets
@router.get("/budgets")
def budgets(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    a = Analysis(db, p.user)
    created = {b.category: b.created_by for b in db.execute(select(Budget).where(Budget.user_id == p.user.id)).scalars()}
    rows = [{**r, "created_by": created.get(r["category"], "user")} for r in a.budget_status]
    return {"status": rows, "suggestions": suggest_budgets(a.txns, a.today), "budgetable": BUDGETABLE}


class BudgetIn(BaseModel):
    monthly_limit_cents: int = Field(ge=0, le=10_000_000)


@router.put("/budgets/{category}")
def set_budget(category: str, body: BudgetIn, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    if category not in BUDGETABLE:
        raise _err(422, "bad_category", "This category cannot have a budget.")
    b = db.execute(select(Budget).where(Budget.user_id == p.user.id, Budget.category == category)).scalar_one_or_none()
    if b is None:
        b = Budget(user_id=p.user.id, category=category, monthly_limit_cents=0, created_by="user")
        db.add(b)
    b.monthly_limit_cents, b.created_by = body.monthly_limit_cents, "user"
    b.updated_at = datetime.now(timezone.utc)
    audit.append(db, p.user.id, "user", "budget.set", {"category": category, "limit_cents": body.monthly_limit_cents})
    db.commit()
    return {"ok": True}


@router.delete("/budgets/{category}")
def delete_budget(category: str, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    b = db.execute(select(Budget).where(Budget.user_id == p.user.id, Budget.category == category)).scalar_one_or_none()
    if b is None:
        raise _err(404, "not_found", "No budget for that category.")
    db.delete(b)
    audit.append(db, p.user.id, "user", "budget.deleted", {"category": category})
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------- goals
@router.get("/goals")
def goals(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    a = Analysis(db, p.user)
    return {"goals": a.goals, "safe_to_save_cents": a.safe_to_save_cents}


class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    target_cents: int = Field(gt=0, le=1_000_000_000)
    target_date: date | None = None


@router.post("/goals", status_code=201)
def create_goal(body: GoalIn, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    count = db.execute(select(func.count(Goal.id)).where(Goal.user_id == p.user.id)).scalar_one()
    if count >= 20:
        raise _err(409, "too_many", "You can have up to 20 goals.")
    savings = db.execute(select(Account).where(Account.user_id == p.user.id, Account.type == "savings")).scalars().first()
    g = Goal(user_id=p.user.id, name=body.name.strip(), target_cents=body.target_cents, target_date=body.target_date,
             account_id=savings.id if savings else None)
    db.add(g)
    db.flush()
    audit.append(db, p.user.id, "user", "goal.created", {"goal_id": g.id, "target_cents": body.target_cents})
    db.commit()
    return {"id": g.id}


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    g = db.get(Goal, goal_id)
    if g is None or g.user_id != p.user.id:
        raise _err(404, "not_found", "Goal not found.")
    db.delete(g)
    audit.append(db, p.user.id, "user", "goal.deleted", {"goal_id": goal_id})
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------- alerts
@router.get("/alerts")
def alerts(p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Alert).where(Alert.user_id == p.user.id).order_by(Alert.id.desc()).limit(100)).scalars()
    return [{"id": x.id, "kind": x.kind, "severity": x.severity, "title": x.title, "body": x.body,
             "date": x.created_on.isoformat(), "read": x.read} for x in rows]


@router.post("/alerts/{alert_id}/read")
def read_alert(alert_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    x = db.get(Alert, alert_id)
    if x is None or x.user_id != p.user.id:
        raise _err(404, "not_found", "Alert not found.")
    x.read = True
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------- payees
@router.get("/payees")
def payees(p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Payee).where(Payee.user_id == p.user.id).order_by(Payee.name)).scalars()
    return [{"id": x.id, "name": x.name, "kind": x.kind, "category": x.category, "reference": x.reference_mask,
             "verified": x.verified, "agent_allowlisted": x.agent_allowlisted} for x in rows]


class PayeeAllowIn(BaseModel):
    agent_allowlisted: bool


@router.patch("/payees/{payee_id}")
def set_allowlist(payee_id: int, body: PayeeAllowIn, p: Principal = Depends(current),
                  db: Session = Depends(get_db)) -> dict:
    x = db.get(Payee, payee_id)
    if x is None or x.user_id != p.user.id:
        raise _err(404, "not_found", "Payee not found.")
    if body.agent_allowlisted and not x.agent_allowlisted:
        require_step_up(p)  # widening what the agent may pay needs fresh re-authentication
        if not x.verified:
            raise _err(409, "unverified", "Only verified billers can be allowlisted.")
    x.agent_allowlisted = body.agent_allowlisted
    audit.append(db, p.user.id, "user", "payee.allowlist_changed", {"payee_id": x.id, "allowlisted": body.agent_allowlisted})
    db.commit()
    return {"ok": True}
