"""Spending anomaly detectors: duplicate charges, fees, outliers, bill spikes, price hikes
and overlapping subscriptions. Each returns explainable Findings with the evidence attached."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from steward.core.categories import FEES, NON_SPENDING
from steward.core.money import fmt
from steward.core.recurring import RecurringSeries
from steward.core.types import Finding, Txn

FEE_PATTERN = re.compile(
    r"\b(late (payment )?(fee|charge)|finance charge|interest charged?|overdraft|fx (transaction )?fee|"
    r"foreign (transaction|currency) fee|cross[- ]border fee|annual fee|card fee|service charge|"
    r"atm (fee|charge)|admin(istrative)? fee|penalty|returned (payment|cheque) fee|fall below fee|"
    r"minimum balance fee)\b",
    re.I,
)

PERIOD_NAME = {
    "weekly": "week", "biweekly": "2 weeks", "monthly": "month", "bimonthly": "2 months",
    "quarterly": "quarter", "semiannual": "6 months", "annual": "year",
}

OVERLAP_GROUPS = {
    "video streaming": {"Netflix", "Disney+", "YouTube Premium", "Amazon Prime", "HBO GO", "Viu Premium"},
    "music streaming": {"Spotify", "Apple Music", "YouTube Music"},
    "cloud storage": {"Apple iCloud", "Google One Storage", "Dropbox Plan"},
}


def find_duplicate_charges(
    txns: list[Txn], today: date, window_days: int = 2, min_amount_cents: int = 1000, lookback_days: int = 60
) -> list[Finding]:
    since = today - timedelta(days=lookback_days)
    outflows = sorted(
        (t for t in txns if t.outflow and t.posted_on >= since and t.category not in NON_SPENDING),
        key=lambda t: (t.posted_on, t.id),
    )
    # Habitual merchants (several visits a month) legitimately repeat the same amount.
    visits = Counter(t.merchant for t in outflows)
    habitual = {m for m, n in visits.items() if n / max(1, lookback_days / 30) >= 6}

    findings = []
    seen: dict[tuple[str, int], list[Txn]] = defaultdict(list)
    for t in outflows:
        if t.abs_cents < min_amount_cents or t.merchant in habitual:
            continue
        key = (t.merchant, t.abs_cents)
        prior = [p for p in seen[key] if (t.posted_on - p.posted_on).days <= window_days]
        if prior:
            original = prior[-1]
            findings.append(
                Finding(
                    kind="duplicate_charge",
                    severity="warning",
                    title=f"Possible duplicate charge at {t.merchant}",
                    detail=(
                        f"{t.merchant} charged {fmt(t.abs_cents)} on {original.posted_on:%d %b} and again on "
                        f"{t.posted_on:%d %b}. If you only bought once, dispute the second charge."
                    ),
                    amount_cents=t.abs_cents,
                    txn_ids=[original.id, t.id],
                    merchant=t.merchant,
                    on=t.posted_on,
                )
            )
        seen[key].append(t)
    return findings


def is_fee(t: Txn) -> bool:
    return t.outflow and (t.category == FEES or bool(FEE_PATTERN.search(t.description)))


def find_fees(txns: list[Txn], today: date, lookback_days: int = 90) -> list[Finding]:
    since = today - timedelta(days=lookback_days)
    fees = [t for t in txns if t.posted_on >= since and is_fee(t)]
    if not fees:
        return []
    total = sum(t.abs_cents for t in fees)
    kinds = Counter(t.merchant for t in fees)
    breakdown = ", ".join(f"{m} ×{n}" for m, n in kinds.most_common(4))
    return [
        Finding(
            kind="fees",
            severity="warning" if total >= 2000 else "info",
            title=f"{fmt(total)} paid in fees in the last {lookback_days} days",
            detail=f"Avoidable charges: {breakdown}. Autopay and fee-free cards remove most of these.",
            amount_cents=total,
            txn_ids=[t.id for t in fees],
            data={"count": len(fees), "by_merchant": dict(kinds)},
        )
    ]


def modified_z(values: list[int], x: int) -> float:
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad == 0:
        mean_ad = statistics.mean([abs(v - med) for v in values]) or 1
        return (x - med) / (1.253314 * mean_ad)
    return 0.6745 * (x - med) / mad


def find_outliers(
    txns: list[Txn], today: date, lookback_days: int = 180, recent_days: int = 30,
    threshold: float = 3.5, min_amount_cents: int = 5000, min_samples: int = 8,
) -> list[Finding]:
    since = today - timedelta(days=lookback_days)
    by_cat: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        if t.outflow and t.posted_on >= since and t.category not in NON_SPENDING and t.category != FEES:
            by_cat[t.category].append(t)
    findings = []
    recent_since = today - timedelta(days=recent_days)
    for cat, items in by_cat.items():
        if len(items) < min_samples:
            continue
        values = sorted(t.abs_cents for t in items)
        med = int(statistics.median(values))
        p90 = values[int(0.9 * (len(values) - 1))]
        for t in items:
            if t.posted_on < recent_since or t.abs_cents < min_amount_cents:
                continue
            z = modified_z(values, t.abs_cents)
            # Robust z alone flags every restaurant meal in a hawker-heavy category; also
            # require the purchase to dwarf the category's usual *large* spends (p90).
            if z >= threshold and t.abs_cents >= 3 * p90:
                findings.append(
                    Finding(
                        kind="large_purchase",
                        severity="info",
                        title=f"Unusually large {cat.lower()} purchase",
                        detail=(
                            f"{fmt(t.abs_cents)} at {t.merchant} on {t.posted_on:%d %b} is about "
                            f"{t.abs_cents / max(med, 1):.0f}× your typical {cat.lower()} spend of {fmt(med)}."
                        ),
                        amount_cents=t.abs_cents,
                        txn_ids=[t.id],
                        merchant=t.merchant,
                        on=t.posted_on,
                        data={"z_score": round(z, 2), "category_median_cents": med},
                    )
                )
    return findings


def find_bill_spikes(series: list[RecurringSeries], threshold: float = 0.25) -> list[Finding]:
    findings = []
    for s in series:
        if s.kind != "bill" or s.fixed_amount or len(s.history) < 4:
            continue
        *earlier, (last_date, last_amount) = s.history
        base = int(statistics.median(a for _, a in earlier[-6:]))
        if base and (last_amount - base) / base >= threshold:
            pct = (last_amount - base) / base
            findings.append(
                Finding(
                    kind="bill_spike",
                    severity="warning",
                    title=f"{s.merchant} bill is {pct:.0%} higher than usual",
                    detail=(
                        f"Latest {s.merchant} payment was {fmt(last_amount)} vs a typical {fmt(base)}. "
                        "Check the bill for usage changes or new line items."
                    ),
                    amount_cents=last_amount - base,
                    merchant=s.merchant,
                    on=last_date,
                    data={"typical_cents": base, "latest_cents": last_amount, "pct": round(pct, 4)},
                )
            )
    return findings


def find_price_hikes(series: list[RecurringSeries], today: date, within_days: int = 120) -> list[Finding]:
    findings = []
    for s in series:
        if (
            s.direction == "outflow"
            and s.price_change_pct
            and s.price_change_pct > 0
            and s.active
            and s.price_change_date
            and (today - s.price_change_date).days <= within_days
        ):
            per_year = int(round((s.last_amount_cents - (s.previous_amount_cents or 0)) * 365.25 / s.interval_days))
            findings.append(
                Finding(
                    kind="price_hike",
                    severity="warning",
                    title=f"{s.merchant} raised its price by {s.price_change_pct:.0%}",
                    detail=(
                        f"{s.merchant} went from {fmt(s.previous_amount_cents or 0)} to {fmt(s.last_amount_cents)} "
                        f"per {PERIOD_NAME.get(s.cadence, s.cadence)} "
                        f"on {s.price_change_date:%d %b}. That is {fmt(per_year)} more a year."
                    ),
                    amount_cents=per_year,
                    merchant=s.merchant,
                    on=s.price_change_date,
                    data={"old_cents": s.previous_amount_cents, "new_cents": s.last_amount_cents,
                          "pct": round(s.price_change_pct, 4)},
                )
            )
    return findings


def find_subscription_overlaps(series: list[RecurringSeries]) -> list[Finding]:
    active = {s.merchant: s for s in series if s.kind == "subscription" and s.active}
    findings = []
    for group, members in OVERLAP_GROUPS.items():
        hits = [active[m] for m in sorted(members) if m in active]
        if len(hits) < 2:
            continue
        monthly = sum(h.monthly_equivalent_cents for h in hits)
        cheapest = min(hits, key=lambda h: h.monthly_equivalent_cents)
        findings.append(
            Finding(
                kind="subscription_overlap",
                severity="info",
                title=f"{len(hits)} {group} subscriptions",
                detail=(
                    f"You pay for {', '.join(h.merchant for h in hits)}: {fmt(monthly)} a month. "
                    f"Rotating them, keeping one at a time, saves at least {fmt(cheapest.annual_cost_cents)} a year."
                ),
                amount_cents=monthly,
                data={"group": group, "merchants": [h.merchant for h in hits]},
            )
        )
    return findings
