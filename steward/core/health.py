"""Financial health score (0-100): a weighted blend of six explainable components."""

from __future__ import annotations

import statistics
from dataclasses import dataclass


def _lerp(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation through (x, score) points sorted by x."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


@dataclass
class HealthInputs:
    monthly_income_cents: int
    monthly_spend_cents: list[int]          # last N full months, total spending
    monthly_essential_cents: int            # typical needs spend per month
    liquid_savings_cents: int               # savings accounts
    fees_last_90d_cents: int
    fee_count_last_90d: int
    budgets_total: int
    budgets_on_track: int
    debt_cents: int                         # credit balances owed
    discretionary_monthly_cents: list[int]  # wants per month


WEIGHTS = {
    "savings_rate": 0.25,
    "emergency_fund": 0.20,
    "fees": 0.15,
    "budget_adherence": 0.15,
    "spending_stability": 0.10,
    "debt_load": 0.15,
}


def score(inputs: HealthInputs) -> dict:
    comps = {}
    income = inputs.monthly_income_cents
    avg_spend = statistics.mean(inputs.monthly_spend_cents) if inputs.monthly_spend_cents else 0
    savings_rate = (income - avg_spend) / income if income else 0.0
    comps["savings_rate"] = {
        "score": _lerp(savings_rate, [(-0.1, 0), (0.0, 30), (0.1, 65), (0.2, 90), (0.3, 100)]),
        "value": round(savings_rate, 4),
        "label": "Savings rate",
        "explain": f"You keep {savings_rate:.0%} of income after spending (target 20%+).",
    }
    months = inputs.liquid_savings_cents / inputs.monthly_essential_cents if inputs.monthly_essential_cents else 0
    comps["emergency_fund"] = {
        "score": _lerp(months, [(0, 0), (1, 30), (3, 70), (6, 100)]),
        "value": round(months, 2),
        "label": "Emergency fund",
        "explain": f"Savings cover {months:.1f} months of essential costs (target 3–6).",
    }
    comps["fees"] = {
        "score": max(0.0, 100 - 25 * inputs.fee_count_last_90d),
        "value": inputs.fees_last_90d_cents,
        "label": "Avoidable fees",
        "explain": f"{inputs.fee_count_last_90d} fee(s) in the last 90 days.",
    }
    if inputs.budgets_total:
        adherence = inputs.budgets_on_track / inputs.budgets_total
        comps["budget_adherence"] = {
            "score": 100 * adherence,
            "value": round(adherence, 4),
            "label": "Budget adherence",
            "explain": f"{inputs.budgets_on_track} of {inputs.budgets_total} budgets on track this month.",
        }
    else:
        comps["budget_adherence"] = {"score": 50.0, "value": None, "label": "Budget adherence",
                                     "explain": "No budgets yet: set some to score this."}
    disc = [d for d in inputs.discretionary_monthly_cents if d > 0]
    cv = statistics.pstdev(disc) / statistics.mean(disc) if len(disc) >= 2 else 0.0
    comps["spending_stability"] = {
        "score": _lerp(cv, [(0, 100), (0.15, 85), (0.3, 60), (0.6, 20), (1.0, 0)]),
        "value": round(cv, 4),
        "label": "Spending stability",
        "explain": f"Discretionary spend varies {cv:.0%} month to month.",
    }
    debt_ratio = inputs.debt_cents / income if income else (1.0 if inputs.debt_cents else 0.0)
    comps["debt_load"] = {
        "score": _lerp(debt_ratio, [(0, 100), (0.3, 75), (1.0, 20), (2.0, 0)]),
        "value": round(debt_ratio, 4),
        "label": "Debt load",
        "explain": f"Revolving debt is {debt_ratio:.0%} of monthly income.",
    }
    total = sum(WEIGHTS[k] * comps[k]["score"] for k in WEIGHTS)
    total = int(round(total))
    grade = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D" if total >= 40 else "E"
    weakest = sorted(WEIGHTS, key=lambda k: comps[k]["score"])[:2]
    tips = {
        "savings_rate": "Automate a transfer to savings on payday so saving happens first.",
        "emergency_fund": "Build the emergency fund toward 3 months of essentials before other goals.",
        "fees": "Turn on bill autopay (or let Steward pay allowlisted bills) to stop late fees.",
        "budget_adherence": "Review the categories that are over pace and trim one habit.",
        "spending_stability": "Smooth big purchases with a sinking fund instead of one-off spikes.",
        "debt_load": "Pay revolving balances in full; card interest outruns most savings rates.",
    }
    for k in WEIGHTS:
        comps[k]["score"] = round(comps[k]["score"], 1)
        comps[k]["weight"] = WEIGHTS[k]
    return {"score": total, "grade": grade, "components": comps, "focus": [tips[k] for k in weakest]}
