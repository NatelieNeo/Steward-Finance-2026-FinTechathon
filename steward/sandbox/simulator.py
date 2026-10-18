"""Applies the persona to the database: seeds six months of history and advances the
sandbox clock day by day (spending, salary, bills, late charges, then the agent's cycle)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from steward.core.money import add_months, daterange
from steward.models import (
    Account,
    AgentPolicy,
    Alert,
    Bill,
    Budget,
    Goal,
    Payee,
    SandboxProfile,
    SubscriptionState,
    User,
)
from steward.sandbox import bank
from steward.sandbox.persona import BILLERS, bill_line_items, daily_postings, default_params
from steward.security import audit

HISTORY_DAYS = 182


def _accounts(db: Session, user_id: int) -> dict[str, Account]:
    accts = db.execute(select(Account).where(Account.user_id == user_id)).scalars().all()
    return {a.type: a for a in accts}


def _post(db: Session, accts: dict[str, Account], p, on: date, created_by: str, history: bool,
          memory: dict) -> bool:
    account = accts[p.account]
    kwargs = {}
    if p.kind == "transfer":
        kwargs = {"category": "Transfers", "merchant": "Savings Transfer"}
    try:
        bank.post(db, account, on=on, description=p.description, amount_cents=p.amount_cents, kind=p.kind,
                  created_by=created_by, allow_overdraft=history, memory=memory, **kwargs)
        return True
    except bank.InsufficientFunds:
        return False


def _issue_bills(db: Session, user: User, profile: SandboxProfile, d: date, payees: dict[str, Payee]) -> list[Bill]:
    issued = []
    for name, (issue_day, days_to_pay, _cat, _allow) in BILLERS.items():
        if d.day != issue_day:
            continue
        payee = payees[name]
        period = d.strftime("%Y-%m")
        prev = db.execute(
            select(Bill).where(Bill.user_id == user.id, Bill.payee_id == payee.id).order_by(Bill.issued_on.desc())
        ).scalars().first()
        late = bool(prev and prev.paid_on and prev.paid_on > prev.due_on) or bool(prev and prev.status == "overdue")
        late = late and not (prev and prev.late_fee_charged)
        if prev and late:
            prev.late_fee_charged = True
        items = bill_line_items(profile.seed, name, d, profile.params, late_fee=late)
        bill = Bill(user_id=user.id, payee_id=payee.id, period_label=period, issued_on=d,
                    due_on=d + timedelta(days=days_to_pay), amount_cents=sum(i["amount_cents"] for i in items),
                    status="open", line_items=items)
        db.add(bill)
        db.flush()
        issued.append(bill)
    return issued


def seed_user(db: Session, user: User, seed: int, anchor: date) -> dict:
    """Create accounts, payees, goals, policy and ~6 months of history ending at `anchor`."""
    params = default_params(anchor)
    profile = SandboxProfile(user_id=user.id, seed=seed, anchor=anchor, params=params)
    db.add(profile)
    checking = Account(user_id=user.id, name="Everyday Spending", type="checking", mask="4821",
                       currency=user.currency)
    savings = Account(user_id=user.id, name="Rainy-Day Savings", type="savings", mask="7710",
                      currency=user.currency)
    db.add_all([checking, savings])
    db.flush()
    accts = {"checking": checking, "savings": savings}

    payees = {}
    for name, (_d, _p, category, allow) in BILLERS.items():
        payees[name] = Payee(user_id=user.id, name=name, kind="biller", category=category,
                             reference_mask="•••" + str(abs(hash((seed, name))) % 9000 + 1000)[:4],
                             verified=True, agent_allowlisted=allow)
    payees["Orchid Property Mgmt"] = Payee(user_id=user.id, name="Orchid Property Mgmt", kind="biller",
                                           category="Housing", reference_mask="•••2210", verified=True,
                                           agent_allowlisted=False)
    db.add_all(payees.values())
    db.flush()

    memory: dict[str, str] = {}
    start = anchor - timedelta(days=HISTORY_DAYS)
    late_month = params["late_bill_month"]
    latest_bill_issues = {}
    for name, (issue_day, _days_to_pay, _category, _allowlisted) in BILLERS.items():
        issue_month = anchor if anchor.day >= issue_day else add_months(anchor, -1)
        latest_bill_issues[name] = issue_month.replace(day=issue_day)
    for d in daterange(start, anchor):
        for p in daily_postings(seed, d, params, cancelled=set(), history=True):
            _post(db, accts, p, d, "seed", history=True, memory=memory)
        _issue_bills(db, user, profile, d, payees)
        # Keep each biller's latest statement open for the agent; pay older history 4 days
        # after issue (one Singtel bill late).
        open_bills = db.execute(
            select(Bill).where(Bill.user_id == user.id, Bill.status == "open", Bill.issued_on <= d)
        ).scalars().all()
        for bill in open_bills:
            payee = db.get(Payee, bill.payee_id)
            if bill.issued_on == latest_bill_issues[payee.name]:
                continue
            paid_on = bill.issued_on + timedelta(days=4)
            if payee.name == "Singtel" and bill.period_label == late_month:
                paid_on = bill.due_on + timedelta(days=5)
            if d == paid_on:
                bank.pay_biller(db, checking, payee, bill.amount_cents, d, bill.period_label, created_by="seed")
                bill.status, bill.paid_on, bill.paid_by = "paid", d, "user"

    # Calibrate opening balances so today's balances are realistic.
    for key, target in (("checking", params["checking_target_cents"]), ("savings", params["savings_target_cents"])):
        acct = accts[key]
        acct.opening_balance_cents = target - acct.balance_cents
        acct.balance_cents = target

    db.add_all([
        Goal(user_id=user.id, name="Emergency Fund", target_cents=1_500_000, saved_cents=620_000,
             account_id=savings.id),
        Goal(user_id=user.id, name="Japan Trip", target_cents=300_000, saved_cents=80_000,
             target_date=date(anchor.year + (1 if anchor.month > 3 else 0), 3, 31), account_id=savings.id),
        Budget(user_id=user.id, category="Dining", monthly_limit_cents=45_000, created_by="user"),
        Budget(user_id=user.id, category="Shopping", monthly_limit_cents=25_000, created_by="user"),
        AgentPolicy(user_id=user.id, tier=1),
    ])
    user.sim_today = anchor
    db.flush()
    audit.append(db, user.id, "sandbox", "sandbox.seeded",
                 {"seed": seed, "anchor": anchor.isoformat(), "history_days": HISTORY_DAYS})
    return {"accounts": 2, "anchor": anchor.isoformat()}


def advance(
    db: Session,
    user: User,
    days: int,
    run_cycle: Callable[[Session, User], dict] | None = None,
) -> list[dict]:
    """Simulate `days` more days: post the day's activity, issue bills, flag overdue
    bills, then let the agent run its daily cycle under the user's current policy."""
    profile = db.get(SandboxProfile, user.id)
    if profile is None:
        raise ValueError("user has no sandbox profile")
    accts = _accounts(db, user.id)
    payees = {p.name: p for p in db.execute(select(Payee).where(Payee.user_id == user.id)).scalars()}
    memory = {}
    log = []
    for _ in range(days):
        d = (user.sim_today or profile.anchor) + timedelta(days=1)
        user.sim_today = d
        cancelled = set(db.execute(
            select(SubscriptionState.merchant).where(SubscriptionState.user_id == user.id,
                                                     SubscriptionState.status == "cancelled")
        ).scalars())
        posted, declined = 0, []
        for p in daily_postings(profile.seed, d, profile.params, cancelled, history=False):
            if _post(db, accts, p, d, "sandbox", history=False, memory=memory):
                posted += 1
            else:
                declined.append(p.description)
        for desc in declined:
            db.add(Alert(user_id=user.id, kind="declined", severity="warning", title="Card payment declined",
                         body=f"{desc} was declined for insufficient funds.",
                         dedupe_key=f"declined:{d}:{desc}"[:120], created_on=d))
        new_bills = _issue_bills(db, user, profile, d, payees)
        overdue = db.execute(
            select(Bill).where(Bill.user_id == user.id, Bill.status == "open", Bill.due_on < d)
        ).scalars().all()
        for b in overdue:
            b.status = "overdue"
            payee = db.get(Payee, b.payee_id)
            db.add(Alert(user_id=user.id, kind="bill_overdue", severity="critical",
                         title=f"{payee.name} bill is overdue",
                         body=f"The {b.period_label} bill was due on {b.due_on:%d %b}. A late charge will be added "
                              "to the next bill.", dedupe_key=f"overdue:{b.id}", created_on=d))
        db.flush()
        audit.append(db, user.id, "sandbox", "sandbox.day_simulated",
                     {"date": d.isoformat(), "postings": posted, "declined": len(declined),
                      "bills_issued": [b.id for b in new_bills], "bills_overdue": [b.id for b in overdue]})
        cycle = run_cycle(db, user) if run_cycle else None
        log.append({"date": d.isoformat(), "postings": posted, "declined": declined,
                    "bills_issued": [{"id": b.id, "amount_cents": b.amount_cents} for b in new_bills],
                    "bills_overdue": [b.id for b in overdue], "agent": cycle})
    return log


def next_month_label(d: date) -> str:
    return add_months(d, 1).strftime("%Y-%m")
