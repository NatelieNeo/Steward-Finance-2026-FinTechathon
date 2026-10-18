"""Typed parameters for every agent action. Anything the LLM (or any client) proposes is
validated against these models before the policy engine sees it: unknown fields, wrong
types and out-of-range values are rejected, never coerced into something executable."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steward.core.categories import BUDGETABLE, CATEGORIES


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CategorizeParams(_Params):
    transaction_id: int = Field(gt=0)
    category: str

    @field_validator("category")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v


class SetBudgetParams(_Params):
    category: str
    monthly_limit_cents: int = Field(ge=0, le=10_000_000)

    @field_validator("category")
    @classmethod
    def _budgetable(cls, v: str) -> str:
        if v not in BUDGETABLE:
            raise ValueError(f"{v!r} cannot have a budget")
        return v


class ReminderParams(_Params):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=500)
    severity: str = Field(default="info", pattern="^(info|warning)$")


class TransferParams(_Params):
    amount_cents: int = Field(gt=0, le=100_000_000)
    from_account_id: int | None = Field(default=None, gt=0)
    to_account_id: int | None = Field(default=None, gt=0)
    goal_id: int | None = Field(default=None, gt=0)
    memo: str = Field(default="", max_length=60)


class PayBillParams(_Params):
    bill_id: int = Field(gt=0)
    amount_cents: int | None = Field(default=None, gt=0, le=100_000_000)
    from_account_id: int | None = Field(default=None, gt=0)


class CancelParams(_Params):
    merchant: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=200)


class DisputeParams(_Params):
    transaction_id: int = Field(gt=0)
    reason: str = Field(default="Unrecognised or duplicate charge", max_length=200)


PARAM_MODELS: dict[str, type[_Params]] = {
    "categorize_transaction": CategorizeParams,
    "set_budget": SetBudgetParams,
    "create_reminder": ReminderParams,
    "transfer_to_savings": TransferParams,
    "pay_bill": PayBillParams,
    "cancel_subscription": CancelParams,
    "dispute_charge": DisputeParams,
}
