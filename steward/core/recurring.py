"""Recurring payment detection: subscriptions, bills and income streams.

For each merchant (split by direction) we look at the gaps between payments, match the
median gap to a known cadence, and score how regular the gaps and amounts are. Series that
are regular enough become RecurringSeries with a predicted next date and amount, a monthly
equivalent cost, and any price change on the latest payments.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from steward.core.categories import BILL_CATEGORIES, NON_SPENDING, SUBSCRIPTION_CATEGORIES
from steward.core.money import add_months
from steward.core.types import Txn

# name, nominal days, tolerance days, months per step (None = step by days)
CADENCES: list[tuple[str, float, float, int | None]] = [
    ("weekly", 7.0, 1.5, None),
    ("biweekly", 14.0, 2.5, None),
    ("monthly", 30.44, 4.0, 1),
    ("bimonthly", 60.9, 6.0, 2),
    ("quarterly", 91.3, 10.0, 3),
    ("semiannual", 182.6, 15.0, 6),
    ("annual", 365.25, 20.0, 12),
]

KNOWN_SUBSCRIPTIONS = {
    "Netflix", "Spotify", "Disney+", "YouTube Premium", "Apple iCloud", "Amazon Prime", "Anytime Fitness",
}

SKIP_CATEGORIES = {"Transfers", "Savings"}


@dataclass
class RecurringSeries:
    merchant: str
    category: str
    direction: str  # outflow | inflow
    cadence: str
    interval_days: float
    occurrences: int
    first_date: date
    last_date: date
    last_amount_cents: int
    typical_amount_cents: int
    amount_cv: float
    fixed_amount: bool
    next_expected_date: date
    next_expected_amount_cents: int
    monthly_equivalent_cents: int
    annual_cost_cents: int
    confidence: float
    kind: str  # subscription | bill | income | other
    active: bool
    previous_amount_cents: int | None = None
    price_change_pct: float | None = None
    price_change_date: date | None = None
    history: list[tuple[date, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "merchant": self.merchant,
            "category": self.category,
            "direction": self.direction,
            "cadence": self.cadence,
            "occurrences": self.occurrences,
            "first_date": self.first_date.isoformat(),
            "last_date": self.last_date.isoformat(),
            "last_amount_cents": self.last_amount_cents,
            "typical_amount_cents": self.typical_amount_cents,
            "fixed_amount": self.fixed_amount,
            "next_expected_date": self.next_expected_date.isoformat(),
            "next_expected_amount_cents": self.next_expected_amount_cents,
            "monthly_equivalent_cents": self.monthly_equivalent_cents,
            "annual_cost_cents": self.annual_cost_cents,
            "confidence": round(self.confidence, 3),
            "kind": self.kind,
            "active": self.active,
            "previous_amount_cents": self.previous_amount_cents,
            "price_change_pct": None if self.price_change_pct is None else round(self.price_change_pct, 4),
            "price_change_date": self.price_change_date.isoformat() if self.price_change_date else None,
            "history": [{"date": d.isoformat(), "amount_cents": a} for d, a in self.history[-12:]],
        }


def _match_cadence(median_gap: float):
    for name, nominal, tol, months in CADENCES:
        if abs(median_gap - nominal) <= tol:
            return name, nominal, tol, months
    return None


def _step(d: date, nominal: float, months: int | None, anchor_day: int) -> date:
    if months:
        return add_months(d, months, day=anchor_day)
    return d + timedelta(days=round(nominal))


def _regularity(gaps: list[int], nominal: float, tol: float) -> float:
    """Share of gaps that fit the cadence. A late payment followed by an early one (two gaps
    summing to two periods) counts as regular, so one slip does not hide a real bill."""
    ok, i, n = 0, 0, len(gaps)
    while i < n:
        if abs(gaps[i] - nominal) <= tol:
            ok, i = ok + 1, i + 1
        elif i + 1 < n and abs(gaps[i] + gaps[i + 1] - 2 * nominal) <= 2 * tol:
            ok, i = ok + 2, i + 2
        else:
            i += 1
    return ok / n if n else 0.0


def _price_change(amounts: list[int], dates: list[date]):
    """Detect a step change at the end of a fixed-price series.

    Returns (previous_amount, pct_change, change_date) or None. The trailing run of
    (near-)equal amounts is the current price; the amount before it is the old price.
    """
    if len(amounts) < 3:
        return None
    last = amounts[-1]
    i = len(amounts) - 1
    while i > 0 and abs(amounts[i - 1] - last) <= 1:
        i -= 1
    if i == 0:
        return None
    previous = amounts[i - 1]
    # The old price must have been a fixed price (identical charges), otherwise this is a
    # variable bill whose latest amount simply differs.
    earlier = amounts[:i]
    if max(earlier) - min(earlier) > 2:
        return None
    if previous <= 0:
        return None
    pct = (last - previous) / previous
    if abs(pct) < 0.02:
        return None
    return previous, pct, dates[i]


def detect_recurring(txns: list[Txn], today: date, min_occurrences: int = 3) -> list[RecurringSeries]:
    groups: dict[tuple[str, str], list[Txn]] = defaultdict(list)
    for t in txns:
        if t.category in SKIP_CATEGORIES or t.amount_cents == 0 or t.posted_on > today:
            continue
        if "prompt_injection_suspected" in t.flags:
            continue
        direction = "outflow" if t.amount_cents < 0 else "inflow"
        groups[(t.merchant, direction)].append(t)

    series: list[RecurringSeries] = []
    for (merchant, direction), items in groups.items():
        items.sort(key=lambda t: t.posted_on)
        # Collapse same-day duplicates for interval purposes (a double charge is not a cadence).
        dated: list[Txn] = []
        for t in items:
            if dated and dated[-1].posted_on == t.posted_on:
                continue
            dated.append(t)
        if len(dated) < 2:
            continue
        gaps = [(b.posted_on - a.posted_on).days for a, b in zip(dated, dated[1:], strict=False)]
        median_gap = statistics.median(gaps)
        match = _match_cadence(median_gap)
        if not match:
            continue
        cadence, nominal, tol, months = match
        needed = min_occurrences if nominal < 60 else 2
        if len(dated) < needed:
            continue
        regular = _regularity(gaps, nominal, tol)
        if regular < 0.6:
            continue

        amounts = [t.abs_cents for t in dated]
        mean_amount = statistics.mean(amounts)
        cv = statistics.pstdev(amounts) / mean_amount if mean_amount else 1.0
        category = statistics.mode([t.category for t in dated])
        if category in NON_SPENDING and direction == "outflow":
            continue

        change = _price_change(amounts, [t.posted_on for t in dated])
        # Fixed if the amounts are flat, or flat apart from one clean price step.
        fixed = cv <= 0.02 or change is not None
        if direction == "inflow":
            ok = cv <= 0.30
        elif category in BILL_CATEGORIES:
            ok = cv <= 0.60
        else:
            ok = fixed or cv <= 0.10
        if not ok:
            continue

        last = dated[-1]
        anchor_day = statistics.mode([t.posted_on.day for t in dated]) if months else last.posted_on.day
        next_date = _step(last.posted_on, nominal, months, anchor_day)
        typical = int(statistics.median(amounts))
        if fixed:
            next_amount = last.abs_cents
        else:
            next_amount = int(statistics.median(amounts[-3:]))
        monthly_eq = int(round(next_amount * 30.44 / nominal))
        # Grace window: a late payment (e.g. a bill still open) should not mark the series dead.
        active = today <= next_date + timedelta(days=round(max(2 * tol, 0.5 * nominal)))

        if direction == "inflow":
            kind = "income"
        elif (category in SUBSCRIPTION_CATEGORIES or merchant in KNOWN_SUBSCRIPTIONS) and fixed:
            kind = "subscription"
        elif category in BILL_CATEGORIES:
            kind = "bill"
        else:
            kind = "other"

        confidence = 0.5 * regular + 0.3 * min(1.0, len(dated) / 6) + 0.2 * (1 - min(1.0, cv))
        rs = RecurringSeries(
            merchant=merchant,
            category=category,
            direction=direction,
            cadence=cadence,
            interval_days=nominal,
            occurrences=len(dated),
            first_date=dated[0].posted_on,
            last_date=last.posted_on,
            last_amount_cents=last.abs_cents,
            typical_amount_cents=typical,
            amount_cv=round(cv, 4),
            fixed_amount=fixed,
            next_expected_date=next_date,
            next_expected_amount_cents=next_amount,
            monthly_equivalent_cents=monthly_eq,
            annual_cost_cents=int(round(next_amount * 365.25 / nominal)),
            confidence=confidence,
            kind=kind,
            active=active,
            history=[(t.posted_on, t.abs_cents) for t in dated],
        )
        if change and direction == "outflow":
            rs.previous_amount_cents, rs.price_change_pct, rs.price_change_date = change
        series.append(rs)

    series.sort(key=lambda s: (s.direction != "inflow", -s.monthly_equivalent_cents))
    return series


def project_occurrences(s: RecurringSeries, start: date, end: date) -> list[date]:
    """Expected dates of a series strictly after `start` and up to `end`."""
    _, nominal, _, months = next(c for c in CADENCES if c[0] == s.cadence)
    anchor_day = s.next_expected_date.day if months else 0
    out = []
    d = s.next_expected_date
    guard = 0
    while d <= end and guard < 400:
        if d > start:
            out.append(d)
        d = _step(d, nominal, months, anchor_day)
        guard += 1
    return out
