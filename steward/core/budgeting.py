"""Smart budgeting: data-driven budget suggestions, a 50/30/20 check, and pace tracking."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from steward.core.categories import BUDGETABLE, NEEDS, NON_SPENDING, WANTS, bucket
from steward.core.money import days_in_month, month_key, previous_months, round_up_to
from steward.core.types import Txn


def monthly_category_totals(txns: list[Txn]) -> dict[str, dict[str, int]]:
    """{month_key: {category: spent_cents}} for spending (outflows outside NON_SPENDING)."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in txns:
        if t.outflow and t.category not in NON_SPENDING:
            out[month_key(t.posted_on)][t.category] += t.abs_cents
        elif t.amount_cents > 0 and t.category not in {"Income", "Transfers", "Savings"}:
            # Refunds into a spending category reduce that month's spend.
            out[month_key(t.posted_on)][t.category] -= t.amount_cents
    return {m: dict(c) for m, c in out.items()}


def monthly_income(txns: list[Txn]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for t in txns:
        if t.amount_cents > 0 and t.category == "Income":
            out[month_key(t.posted_on)] += t.amount_cents
    return dict(out)


def typical_monthly_income(txns: list[Txn], today: date, months: int = 3) -> int:
    incomes = monthly_income(txns)
    vals = [incomes.get(m, 0) for m in previous_months(today, months)]
    vals = [v for v in vals if v > 0]
    return int(statistics.median(vals)) if vals else 0


def suggest_budgets(txns: list[Txn], today: date, months: int = 3) -> dict:
    """Budget per category = median of the last `months` full months, rounded up to S$10.

    If wants exceed 30% of income (the 50/30/20 guideline), discretionary suggestions are
    trimmed proportionally, never below 70% of history (budgets nobody can keep get ignored).
    """
    totals = monthly_category_totals(txns)
    keys = previous_months(today, months)
    income = typical_monthly_income(txns, today, months)
    suggestions = []
    for cat in BUDGETABLE:
        history = [max(0, totals.get(k, {}).get(cat, 0)) for k in keys]
        if not any(history):
            continue
        base = int(statistics.median(history))
        if base < 1000:
            continue
        suggestions.append({"category": cat, "bucket": bucket(cat), "history_cents": history,
                            "basis_cents": base, "suggested_cents": round_up_to(base, 1000)})

    needs = sum(s["basis_cents"] for s in suggestions if s["category"] in NEEDS)
    wants = sum(s["basis_cents"] for s in suggestions if s["category"] in WANTS)
    fees = sum(s["basis_cents"] for s in suggestions if s["bucket"] == "fees")
    trim = 1.0
    if income and wants > 0.30 * income:
        trim = max(0.70, (0.30 * income) / wants)
    for s in suggestions:
        if s["category"] in WANTS and trim < 1.0:
            s["suggested_cents"] = round_up_to(int(s["basis_cents"] * trim), 1000)
            s["note"] = f"Trimmed {1 - trim:.0%} to bring wants toward 30% of income."
        elif s["bucket"] == "fees":
            s["suggested_cents"] = 0
            s["note"] = "Fees are avoidable; aim for zero."
        else:
            s["note"] = "Based on your median month."
    planned_spend = sum(s["suggested_cents"] for s in suggestions)
    return {
        "months_used": keys,
        "monthly_income_cents": income,
        "suggestions": suggestions,
        "fifty_thirty_twenty": {
            "needs_cents": needs,
            "wants_cents": wants,
            "fees_cents": fees,
            "savings_cents": income - needs - wants - fees if income else 0,
            "needs_pct": round(needs / income, 4) if income else None,
            "wants_pct": round(wants / income, 4) if income else None,
            "savings_pct": round((income - needs - wants - fees) / income, 4) if income else None,
            "targets": {"needs_pct": 0.5, "wants_pct": 0.3, "savings_pct": 0.2},
        },
        "planned_savings_cents": income - planned_spend if income else 0,
    }


def budget_status(
    budgets: dict[str, int],
    txns: list[Txn],
    today: date,
    scheduled_remaining: dict[str, int] | None = None,
    recurring_merchants: set[str] | None = None,
    history_months: int = 3,
) -> list[dict]:
    """Month-to-date spend vs budget, with a month-end projection.

    projection = spent so far
               + known recurring charges still due this month (scheduled_remaining)
               + remaining days x blended daily rate of *flexible* (non-recurring) spend,
                 where the blend moves from the historical rate early in the month to this
                 month's observed rate. Rent paid on the 1st is never extrapolated.
    """
    scheduled_remaining = scheduled_remaining or {}
    recurring_merchants = recurring_merchants or set()
    current = month_key(today)
    totals = monthly_category_totals(txns)
    flexible = monthly_category_totals([t for t in txns if t.merchant not in recurring_merchants])
    spent_now = totals.get(current, {})
    flex_now = flexible.get(current, {})
    keys = previous_months(today, history_months)
    dim = days_in_month(today)
    elapsed = today.day
    remaining_days = dim - elapsed
    frac = elapsed / dim
    rows = []
    for cat, limit in sorted(budgets.items()):
        spent = max(0, spent_now.get(cat, 0))
        scheduled = scheduled_remaining.get(cat, 0)
        flex_hist = [max(0, flexible.get(k, {}).get(cat, 0)) for k in keys]
        flex_monthly = statistics.median(flex_hist) if flex_hist else 0
        observed_rate = max(0, flex_now.get(cat, 0)) / elapsed if elapsed else 0
        historical_rate = flex_monthly / dim
        # No history for this category yet: trust this month's pace rather than assume zero.
        rate = observed_rate if not any(flex_hist) else frac * observed_rate + (1 - frac) * historical_rate
        projected = int(round(spent + scheduled + rate * remaining_days))
        if spent > limit:
            status = "over"
        elif projected > limit:
            status = "at_risk"
        else:
            status = "on_track"
        rows.append({
            "category": cat,
            "limit_cents": limit,
            "spent_cents": spent,
            "remaining_cents": limit - spent,
            "pct_used": round(spent / limit, 4) if limit else None,
            "month_elapsed_pct": round(frac, 4),
            "projected_cents": projected,
            "projected_over_cents": max(0, projected - limit),
            "status": status,
            "pace": round((spent / limit) / frac, 3) if limit and frac else None,
        })
    return rows
