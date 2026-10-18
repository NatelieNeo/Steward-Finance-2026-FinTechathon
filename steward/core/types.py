"""Plain data types shared by the analytics engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Txn:
    id: int
    posted_on: date
    description: str
    merchant: str
    amount_cents: int  # negative = outflow
    category: str = "Other"
    account_id: int = 0
    kind: str = "purchase"
    flags: tuple[str, ...] = ()

    @property
    def outflow(self) -> bool:
        return self.amount_cents < 0

    @property
    def abs_cents(self) -> int:
        return abs(self.amount_cents)


@dataclass
class Finding:
    kind: str
    severity: str  # info | warning | critical
    title: str
    detail: str
    amount_cents: int = 0
    txn_ids: list[int] = field(default_factory=list)
    merchant: str | None = None
    on: date | None = None
    data: dict = field(default_factory=dict)
