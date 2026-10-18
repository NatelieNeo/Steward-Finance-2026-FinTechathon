"""Savings goals and the safe-to-save calculation the agent uses for autonomous sweeps."""

from __future__ import annotations

import math
from datetime import date

from steward.core.forecast import Forecast
from steward.core.money import add_months


def months_between(a: date, b: date) -> float:
    return max(0.0, (b - a).days / 30.44)


def goal_progress(name: str, target_cents: int, saved_cents: int, target_date: date | None,
                  today: date, avg_monthly_contribution_cents: int) -> dict:
    remaining = max(0, target_cents - saved_cents)
    progress = saved_cents / target_cents if target_cents else 1.0
    out = {
        "name": name,
        "target_cents": target_cents,
        "saved_cents": saved_cents,
        "remaining_cents": remaining,
        "progress_pct": round(min(1.0, progress), 4),
        "target_date": target_date.isoformat() if target_date else None,
        "avg_monthly_contribution_cents": avg_monthly_contribution_cents,
    }
    if target_date:
        months_left = months_between(today, target_date)
        required = int(math.ceil(remaining / months_left)) if months_left >= 1 else remaining
        out["months_left"] = round(months_left, 1)
        out["required_monthly_cents"] = required
        out["on_track"] = avg_monthly_contribution_cents >= required or remaining == 0
    if avg_monthly_contribution_cents > 0 and remaining > 0:
        months_needed = math.ceil(remaining / avg_monthly_contribution_cents)
        out["projected_completion"] = add_months(today, months_needed).isoformat()
    elif remaining == 0:
        out["projected_completion"] = today.isoformat()
    else:
        out["projected_completion"] = None
    return out


def safe_to_save(fc: Forecast, max_fraction: float = 0.5, step_cents: int = 1000) -> int:
    """Amount that can be moved to savings today while the pessimistic (P10) path of the
    whole forecast stays above the user's floor. Only `max_fraction` of the headroom is
    used, rounded down to S$10, so a bad month still has slack."""
    headroom = fc.min_low_cents - fc.floor_cents
    if headroom <= 0:
        return 0
    amount = int(headroom * max_fraction)
    return (amount // step_cents) * step_cents
