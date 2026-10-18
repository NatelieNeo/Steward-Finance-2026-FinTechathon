"""Money helpers. Amounts are integer cents end to end; Decimal is used only at the edges."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SYMBOLS = {"SGD": "S$", "USD": "US$", "EUR": "€", "GBP": "£", "MYR": "RM", "AUD": "A$"}


def to_cents(value: str | int | float | Decimal) -> int:
    """Convert a major-unit amount (e.g. "12.345" or 12.3) to integer cents, half-up."""
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"not a money amount: {value!r}")
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def fmt(cents: int, currency: str = "SGD") -> str:
    sign = "-" if cents < 0 else ""
    symbol = SYMBOLS.get(currency, currency + " ")
    return f"{sign}{symbol}{abs(cents) / 100:,.2f}"


def pct(part: float, whole: float) -> float:
    return 0.0 if not whole else part / whole


def round_up_to(cents: int, step_cents: int) -> int:
    if cents <= 0:
        return 0
    return ((cents + step_cents - 1) // step_cents) * step_cents


def add_months(d: date, months: int, day: int | None = None) -> date:
    """Shift a date by whole months, clamping to the month's last day."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day or d.day, last))


def month_start(d: date) -> date:
    return d.replace(day=1)


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def previous_months(today: date, count: int, include_current: bool = False) -> list[str]:
    """Month keys, oldest first. Full months before `today`, optionally plus the current one."""
    keys = []
    start = 0 if include_current else 1
    for i in range(start, start + count):
        keys.append(month_key(add_months(month_start(today), -i)))
    return list(reversed(keys))


def daterange(start: date, end: date):
    """Inclusive range of dates."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
