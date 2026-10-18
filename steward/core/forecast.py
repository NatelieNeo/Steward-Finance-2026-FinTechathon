"""Probabilistic cash-flow forecast.

balance(t) = balance(t-1) + scheduled_events(t) - discretionary(t)

* Scheduled events: predicted occurrences of active recurring series (salary, rent,
  subscriptions) plus open bill statements on their due dates.
* Discretionary spend: a recency-weighted mean and variance of day-of-week spending over
  the last 12 weeks, excluding anything recurring.
* Uncertainty accumulates as a random walk (variances add), giving a P10-P90 band and a
  per-day probability that the balance goes below zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from steward.core.categories import NON_SPENDING
from steward.core.recurring import RecurringSeries, project_occurrences
from steward.core.types import Txn

Z_P90 = 1.2816
ONE_OFF_FLOOR_CENTS = 30_000


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class ScheduledEvent:
    on: date
    amount_cents: int  # signed
    label: str
    source: str  # recurring | bill

    def to_dict(self) -> dict:
        return {"date": self.on.isoformat(), "amount_cents": self.amount_cents, "label": self.label,
                "source": self.source}


@dataclass
class ForecastPoint:
    on: date
    expected_cents: int
    low_cents: int
    high_cents: int
    p_negative: float
    events: list[ScheduledEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "date": self.on.isoformat(),
            "expected_cents": self.expected_cents,
            "low_cents": self.low_cents,
            "high_cents": self.high_cents,
            "p_negative": round(self.p_negative, 4),
            "events": [e.to_dict() for e in self.events],
        }


@dataclass
class Forecast:
    today: date
    start_balance_cents: int
    horizon_days: int
    points: list[ForecastPoint]
    daily_discretionary_cents: int
    next_income_date: date | None
    floor_cents: int

    @property
    def min_point(self) -> ForecastPoint:
        return min(self.points, key=lambda p: p.expected_cents) if self.points else None

    @property
    def min_low_cents(self) -> int:
        return min((p.low_cents for p in self.points), default=self.start_balance_cents)

    @property
    def overdraft_probability(self) -> float:
        return max((p.p_negative for p in self.points), default=0.0)

    def low_until(self, until: date) -> int:
        lows = [p.low_cents for p in self.points if p.on <= until]
        return min(lows, default=self.start_balance_cents)

    @property
    def safe_to_spend_cents(self) -> int:
        """What can be spent before the next payday while the pessimistic (P10) path stays
        above the floor. Without a known payday the whole horizon is used."""
        until = self.next_income_date or (self.today + timedelta(days=self.horizon_days))
        return max(0, self.low_until(until) - self.floor_cents)

    @property
    def upcoming(self) -> list[ScheduledEvent]:
        return [e for p in self.points for e in p.events]

    def to_dict(self) -> dict:
        mp = self.min_point
        return {
            "today": self.today.isoformat(),
            "start_balance_cents": self.start_balance_cents,
            "horizon_days": self.horizon_days,
            "daily_discretionary_cents": self.daily_discretionary_cents,
            "next_income_date": self.next_income_date.isoformat() if self.next_income_date else None,
            "min_expected_cents": mp.expected_cents if mp else self.start_balance_cents,
            "min_expected_date": mp.on.isoformat() if mp else None,
            "min_low_cents": self.min_low_cents,
            "overdraft_probability": round(self.overdraft_probability, 4),
            "safe_to_spend_cents": self.safe_to_spend_cents,
            "floor_cents": self.floor_cents,
            "points": [p.to_dict() for p in self.points],
            "upcoming": [e.to_dict() for e in self.upcoming],
        }


def discretionary_profile(
    txns: list[Txn], today: date, recurring_merchants: set[str], weeks: int = 12, decay: float = 0.9
) -> dict[int, tuple[float, float]]:
    """Recency-weighted (mean, variance) of daily discretionary spend per weekday (0=Mon)."""
    start = today - timedelta(days=weeks * 7 + 1)
    candidates = [
        t for t in txns
        if t.outflow
        and start < t.posted_on <= today
        and t.category not in NON_SPENDING
        and t.merchant not in recurring_merchants
        and t.kind not in {"bill_payment", "transfer"}
    ]
    # One-off big purchases (a laptop, a flight) are not a daily habit: leave them out of the baseline.
    typical = sorted(t.abs_cents for t in candidates)
    cap = max(ONE_OFF_FLOOR_CENTS, 8 * typical[len(typical) // 2]) if typical else ONE_OFF_FLOOR_CENTS
    daily: dict[date, int] = defaultdict(int)
    for t in candidates:
        if t.abs_cents <= cap:
            daily[t.posted_on] += t.abs_cents
    profile = {}
    for dow in range(7):
        samples = []
        d = today - timedelta(days=1)  # today may be partial: learn from complete days only
        while d.weekday() != dow:
            d -= timedelta(days=1)
        for k in range(weeks):
            day = d - timedelta(days=7 * k)
            if day <= start:
                break
            samples.append((decay ** k, daily.get(day, 0)))
        wsum = sum(w for w, _ in samples) or 1.0
        mean = sum(w * v for w, v in samples) / wsum
        var = sum(w * (v - mean) ** 2 for w, v in samples) / wsum
        profile[dow] = (mean, var)
    return profile


def build_forecast(
    *,
    start_balance_cents: int,
    today: date,
    txns: list[Txn],
    series: list[RecurringSeries],
    open_bills: list[dict] | None = None,
    horizon_days: int = 30,
    floor_cents: int = 0,
    account_ids: set[int] | None = None,
) -> Forecast:
    """open_bills: [{"due_on": date, "amount_cents": int (positive), "label": str, "merchant": str}]"""
    open_bills = open_bills or []
    end = today + timedelta(days=horizon_days)
    if account_ids:
        txns = [t for t in txns if t.account_id in account_ids]

    events: dict[date, list[ScheduledEvent]] = defaultdict(list)
    billed_merchants = {}
    for b in open_bills:
        due = max(b["due_on"], today + timedelta(days=1))
        if due <= end:
            events[due].append(ScheduledEvent(due, -abs(b["amount_cents"]), b["label"], "bill"))
        billed_merchants.setdefault(b["merchant"], []).append(b["due_on"])

    next_income = None
    for s in series:
        if not s.active:
            continue
        for d in project_occurrences(s, today, end):
            # The open statement already covers this cycle of a bill paid by statement.
            if any(abs((d - due).days) <= 20 for due in billed_merchants.get(s.merchant, [])):
                continue
            amount = s.next_expected_amount_cents if s.direction == "inflow" else -s.next_expected_amount_cents
            events[d].append(ScheduledEvent(d, amount, s.merchant, "recurring"))
            if s.direction == "inflow" and s.kind == "income" and amount >= 50_000:
                next_income = min(next_income, d) if next_income else d

    recurring_merchants = {s.merchant for s in series if s.direction == "outflow"}
    profile = discretionary_profile(txns, today, recurring_merchants)

    points = []
    expected = float(start_balance_cents)
    variance = 0.0
    for i in range(1, horizon_days + 1):
        d = today + timedelta(days=i)
        mean, var = profile[d.weekday()]
        day_events = events.get(d, [])
        expected += sum(e.amount_cents for e in day_events) - mean
        variance += var
        sd = math.sqrt(variance)
        p_neg = normal_cdf((0 - expected) / sd) if sd > 0 else (1.0 if expected < 0 else 0.0)
        points.append(
            ForecastPoint(
                on=d,
                expected_cents=int(round(expected)),
                low_cents=int(round(expected - Z_P90 * sd)),
                high_cents=int(round(expected + Z_P90 * sd)),
                p_negative=p_neg,
                events=day_events,
            )
        )
    daily_mean = int(round(sum(m for m, _ in profile.values()) / 7))
    return Forecast(
        today=today,
        start_balance_cents=start_balance_cents,
        horizon_days=horizon_days,
        points=points,
        daily_discretionary_cents=daily_mean,
        next_income_date=next_income,
        floor_cents=floor_cents,
    )


def check_affordability(fc: Forecast, amount_cents: int, on: date | None = None) -> dict:
    """Would a one-off purchase keep the forecast above zero and above the safety floor?"""
    on = on or fc.today + timedelta(days=1)
    after = [p for p in fc.points if p.on >= on]
    if not after:
        after = fc.points[-1:]
    min_expected = min(p.expected_cents for p in after) - amount_cents
    min_low = min(p.low_cents for p in after) - amount_cents
    worst_p = 0.0
    for p in after:
        sd = (p.expected_cents - p.low_cents) / Z_P90 if p.expected_cents != p.low_cents else 0
        e = p.expected_cents - amount_cents
        worst_p = max(worst_p, normal_cdf(-e / sd) if sd else (1.0 if e < 0 else 0.0))
    if min_expected < 0:
        verdict = "not_affordable"
        reason = "Your expected balance would go negative before the end of the forecast."
    elif min_low < fc.floor_cents:
        verdict = "risky"
        reason = "It fits on an average month, but in a bad month your balance could dip below your safety buffer."
    else:
        verdict = "affordable"
        reason = "Even on a pessimistic path your balance stays above your safety buffer."
    return {
        "amount_cents": amount_cents,
        "date": on.isoformat(),
        "verdict": verdict,
        "reason": reason,
        "min_expected_after_cents": min_expected,
        "min_low_after_cents": min_low,
        "floor_cents": fc.floor_cents,
        "overdraft_probability_after": round(worst_p, 4),
        "headroom_cents": min_low - fc.floor_cents,
    }
