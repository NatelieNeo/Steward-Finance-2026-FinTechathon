"""ORM models. Money is always stored as integer minor units (cents), never floats."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from steward.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    # Sandbox clock: the simulated "today" for this user (None = real date).
    sim_today: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    step_up_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str] = mapped_column(String(64), default="")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(16))  # checking | savings | credit
    institution: Mapped[str] = mapped_column(String(80), default="Steward Sandbox Bank")
    mask: Mapped[str] = mapped_column(String(8), default="")
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    opening_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "import_hash", name="uq_txn_import_hash"),
        Index("ix_txn_user_date", "user_id", "posted_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    posted_on: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(255))
    merchant: Mapped[str] = mapped_column(String(120), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)  # negative = money out
    category: Mapped[str] = mapped_column(String(40), index=True, default="Other")
    category_source: Mapped[str] = mapped_column(String(16), default="rule")
    category_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    kind: Mapped[str] = mapped_column(String(16), default="purchase")
    flags: Mapped[list] = mapped_column(JSON, default=list)
    import_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(16), default="seed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MerchantMemory(Base):
    """Category corrections the user made, learned per merchant."""

    __tablename__ = "merchant_memory"
    __table_args__ = (UniqueConstraint("user_id", "merchant", name="uq_memory_merchant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    merchant: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_budget_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(40))
    monthly_limit_cents: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(String(16), default="user")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    target_cents: Mapped[int] = mapped_column(BigInteger)
    saved_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Payee(Base):
    __tablename__ = "payees"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_payee_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16), default="biller")  # biller | person
    category: Mapped[str] = mapped_column(String(40), default="Utilities")
    reference_mask: Mapped[str] = mapped_column(String(16), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_allowlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Bill(Base):
    """A bill statement issued by a biller, payable by the user (or the agent)."""

    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("user_id", "payee_id", "period_label", name="uq_bill_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    payee_id: Mapped[int] = mapped_column(ForeignKey("payees.id", ondelete="CASCADE"), index=True)
    period_label: Mapped[str] = mapped_column(String(20))
    issued_on: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | paid | overdue
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    late_fee_charged: Mapped[bool] = mapped_column(Boolean, default=False)
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubscriptionState(Base):
    __tablename__ = "subscription_states"
    __table_args__ = (UniqueConstraint("user_id", "merchant", name="uq_subscription_merchant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    merchant: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="cancelled")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="provisional_credit")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentPolicy(Base):
    """The permission tier and guardrails the user granted the agent."""

    __tablename__ = "agent_policies"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    tier: Mapped[int] = mapped_column(Integer, default=1)
    per_action_limit_cents: Mapped[int] = mapped_column(BigInteger, default=50_000)
    daily_limit_cents: Mapped[int] = mapped_column(BigInteger, default=100_000)
    monthly_limit_cents: Mapped[int] = mapped_column(BigInteger, default=300_000)
    min_balance_floor_cents: Mapped[int] = mapped_column(BigInteger, default=100_000)
    step_up_threshold_cents: Mapped[int] = mapped_column(BigInteger, default=30_000)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentAction(Base):
    __tablename__ = "agent_actions"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_action_idempotency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(255), default="")
    amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    risk: Mapped[str] = mapped_column(String(4), default="R1")
    # proposed -> executed | pending_approval -> executed/rejected/expired/failed | denied
    status: Mapped[str] = mapped_column(String(20), index=True)
    decision: Mapped[str] = mapped_column(String(20))
    decision_reasons: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    requires_step_up: Mapped[bool] = mapped_column(Boolean, default=False)
    rationale: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="cycle")  # cycle | chat | user
    idempotency_key: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_on: Mapped[date] = mapped_column(Date)  # sandbox date the action was raised
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key", name="uq_alert_dedupe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str] = mapped_column(String(120))
    created_on: Mapped[date] = mapped_column(Date)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEntry(Base):
    """Append-only, hash-chained audit trail (one chain per user)."""

    __tablename__ = "audit_entries"
    __table_args__ = (UniqueConstraint("user_id", "seq", name="uq_audit_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(16))  # user | agent | system | sandbox
    event: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class SandboxProfile(Base):
    """Parameters of the synthetic persona that drives a user's sandbox bank."""

    __tablename__ = "sandbox_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    seed: Mapped[int] = mapped_column(Integer)
    anchor: Mapped[date] = mapped_column(Date)
    params: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
