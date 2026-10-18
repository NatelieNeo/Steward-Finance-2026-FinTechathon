"""Sandbox core-banking ledger.

Every balance change is a posted Transaction, so `opening_balance + sum(transactions)`
must always equal the stored balance; `verify_ledger` checks that invariant. Debits from
deposit accounts can never overdraw them.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from steward.core.categorizer import categorize
from steward.core.merchant import normalize_merchant
from steward.models import Account, MerchantMemory, Payee, Transaction
from steward.security.untrusted import looks_like_injection

QUARANTINED_MERCHANT = "Quarantined Merchant"


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    pass


def _hash(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def classify(db: Session | None, user_id: int, description: str, amount_cents: int,
             memory: dict[str, str] | None = None) -> tuple[str, str, str, float, list[str]]:
    """(merchant, category, source, confidence, flags) for a raw descriptor."""
    flags: list[str] = []
    if looks_like_injection(description):
        flags.append("prompt_injection_suspected")
        return QUARANTINED_MERCHANT, "Other", "quarantine", 1.0, flags
    merchant = normalize_merchant(description)
    if memory is None and db is not None:
        memory = dict(db.execute(
            select(MerchantMemory.merchant, MerchantMemory.category).where(MerchantMemory.user_id == user_id)
        ).all())
    c = categorize(description, merchant, amount_cents, memory=memory)
    return merchant, c.category, c.source, c.confidence, flags


def post(
    db: Session,
    account: Account,
    *,
    on: date,
    description: str,
    amount_cents: int,
    kind: str = "purchase",
    created_by: str = "sandbox",
    category: str | None = None,
    merchant: str | None = None,
    allow_overdraft: bool = False,
    import_hash: str | None = None,
    memory: dict[str, str] | None = None,
) -> Transaction:
    if amount_cents == 0:
        raise LedgerError("zero-amount posting")
    if (
        amount_cents < 0
        and account.type != "credit"
        and not allow_overdraft
        and account.balance_cents + amount_cents < 0
    ):
        raise InsufficientFunds(f"{account.name} has insufficient funds")
    m, cat, source, conf, flags = classify(db, account.user_id, description, amount_cents, memory)
    if merchant and not flags:
        m = merchant
    if category and not flags:
        cat, source, conf = category, "system", 1.0
    txn = Transaction(
        user_id=account.user_id,
        account_id=account.id,
        posted_on=on,
        description=description[:255],
        merchant=m,
        amount_cents=amount_cents,
        category=cat,
        category_source=source,
        category_confidence=conf,
        kind=kind,
        flags=flags,
        import_hash=import_hash or _hash(account.user_id, account.id, on, amount_cents, description,
                                         secrets.token_hex(8)),
        created_by=created_by,
    )
    account.balance_cents += amount_cents
    db.add(txn)
    db.flush()
    return txn


def transfer(db: Session, src: Account, dst: Account, amount_cents: int, on: date, memo: str,
             created_by: str) -> tuple[Transaction, Transaction]:
    if amount_cents <= 0:
        raise LedgerError("transfer amount must be positive")
    if src.id == dst.id or src.user_id != dst.user_id:
        raise LedgerError("transfers are only allowed between two different accounts of the same user")
    out = post(db, src, on=on, description=f"TRANSFER TO {dst.name.upper()} {memo}".strip(),
               amount_cents=-amount_cents, kind="transfer", created_by=created_by,
               category="Transfers", merchant="Savings Transfer")
    inn = post(db, dst, on=on, description=f"TRANSFER FROM {src.name.upper()} {memo}".strip(),
               amount_cents=amount_cents, kind="transfer", created_by=created_by,
               category="Transfers", merchant="Savings Transfer")
    return out, inn


def pay_biller(db: Session, src: Account, payee: Payee, amount_cents: int, on: date, reference: str,
               created_by: str) -> Transaction:
    if amount_cents <= 0:
        raise LedgerError("payment amount must be positive")
    return post(db, src, on=on, description=f"BILL PAYMENT {payee.name.upper()} {reference}".strip(),
                amount_cents=-amount_cents, kind="bill_payment", created_by=created_by,
                category=payee.category, merchant=payee.name)


def verify_ledger(db: Session, user_id: int) -> dict:
    accounts = db.execute(select(Account).where(Account.user_id == user_id)).scalars().all()
    rows = []
    ok = True
    for a in accounts:
        posted = db.execute(
            select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(Transaction.account_id == a.id)
        ).scalar_one()
        expected = a.opening_balance_cents + int(posted)
        match = expected == a.balance_cents
        ok &= match
        rows.append({"account_id": a.id, "name": a.name, "balance_cents": a.balance_cents,
                     "opening_cents": a.opening_balance_cents, "posted_sum_cents": int(posted),
                     "consistent": match})
    return {"ok": ok, "accounts": rows}
