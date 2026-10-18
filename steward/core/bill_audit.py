"""Bill analysis: parse bill text into line items and audit them.

Checks
  * arithmetic   - line items add up to the stated total
  * tax          - GST line is ~9% of the taxable subtotal (Singapore GST since 1 Jan 2024)
  * fees         - late/admin/reconnection/penalty charges
  * new charges  - line items that were not on the previous bill (VAS, roaming, rentals)
  * increases    - line items up more than 20% on the previous bill
  * spike        - total vs the median of earlier bills
"""

from __future__ import annotations

import re
import statistics

from steward.core.money import fmt, to_cents

GST_RATE = 0.09
GST_TOLERANCE_CENTS = 5

FEE_WORDS = re.compile(r"\b(late|penalty|admin(istrative)?|reconnection|surcharge|overdue|interest|fee)\b", re.I)
TAX_WORDS = re.compile(r"\b(gst|vat|tax)\b", re.I)
DISCOUNT_WORDS = re.compile(r"\b(discount|rebate|credit|promo|waiver|offset)\b", re.I)
TOTAL_WORDS = re.compile(r"\b(total|amount due|balance due|amount payable)\b", re.I)
SUBTOTAL_WORDS = re.compile(r"\bsub[- ]?total\b", re.I)
RISKY_NEW = re.compile(r"\b(vas|premium sms|roaming|rental|add[- ]?on|content|third[- ]party|subscription)\b", re.I)

_AMOUNT_AT_END = re.compile(
    r"^(?P<label>.*?)[\s:.\-]*?(?P<neg>\(|-)?\s*(?:S?\$|SGD)?\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})\)?"
    r"\s*(?P<crdr>CR|DR)?\s*$",
    re.I,
)


def classify_line(label: str, amount_cents: int) -> str:
    if TAX_WORDS.search(label):
        return "tax"
    if amount_cents < 0 or DISCOUNT_WORDS.search(label):
        return "discount"
    if FEE_WORDS.search(label):
        return "fee"
    return "charge"


def parse_bill_text(text: str, max_lines: int = 200) -> dict:
    """Extract line items and the stated total from pasted bill text."""
    items: list[dict] = []
    stated_total = None
    for raw in text.splitlines()[:max_lines]:
        line = raw.strip()
        if not line:
            continue
        m = _AMOUNT_AT_END.match(line)
        if not m or not m.group("label").strip():
            continue
        label = re.sub(r"\s+", " ", m.group("label")).strip(" :-.")[:80]
        cents = to_cents(m.group("num").replace(",", ""))
        if m.group("neg") or (m.group("crdr") or "").upper() == "CR":
            cents = -cents
        if SUBTOTAL_WORDS.search(label):
            continue
        if TOTAL_WORDS.search(label):
            stated_total = cents
            continue
        items.append({"label": label, "amount_cents": cents, "type": classify_line(label, cents)})
    return {"line_items": items, "stated_total_cents": stated_total}


def _norm(label: str) -> str:
    """Comparable key for a line item across bills (drops usage figures and rates)."""
    head = label.lower().split("@")[0]
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", head)).strip()[:40]


def audit_bill(
    line_items: list[dict],
    stated_total_cents: int | None = None,
    previous_items: list[dict] | None = None,
    history_totals_cents: list[int] | None = None,
    payee: str = "this biller",
) -> dict:
    findings: list[dict] = []
    total = sum(i["amount_cents"] for i in line_items)

    if stated_total_cents is not None and abs(stated_total_cents - total) > 1:
        diff = stated_total_cents - total
        findings.append({
            "kind": "arithmetic", "severity": "critical",
            "title": "Line items do not add up to the total",
            "detail": f"Items sum to {fmt(total)} but the bill asks for {fmt(stated_total_cents)} "
                      f"({fmt(diff)} unexplained).",
            "amount_cents": abs(diff),
        })

    taxes = [i for i in line_items if i.get("type") == "tax"]
    taxable = sum(i["amount_cents"] for i in line_items if i.get("type") in {"charge", "fee", "discount"})
    if taxes and taxable > 0:
        expected = round(taxable * GST_RATE)
        charged = sum(t["amount_cents"] for t in taxes)
        if abs(charged - expected) > GST_TOLERANCE_CENTS:
            findings.append({
                "kind": "tax", "severity": "warning",
                "title": "GST does not match 9% of the taxable amount",
                "detail": f"GST charged {fmt(charged)}; 9% of {fmt(taxable)} is {fmt(expected)}.",
                "amount_cents": abs(charged - expected),
            })

    for i in line_items:
        if i.get("type") == "fee":
            findings.append({
                "kind": "fee", "severity": "warning",
                "title": f"Fee on the bill: {i['label']}",
                "detail": f"{fmt(i['amount_cents'])}. Fees like this can usually be waived once on request, "
                          "and autopay prevents late charges.",
                "amount_cents": i["amount_cents"],
            })

    if previous_items is not None:
        prev = {_norm(p["label"]): p["amount_cents"] for p in previous_items}
        for i in line_items:
            key = _norm(i["label"])
            if i.get("type") in {"tax", "discount", "fee"}:  # fees are already flagged above
                continue
            if key not in prev:
                risky = bool(RISKY_NEW.search(i["label"]))
                findings.append({
                    "kind": "new_charge", "severity": "warning" if risky else "info",
                    "title": f"New line item: {i['label']}",
                    "detail": f"{fmt(i['amount_cents'])} did not appear on the previous bill"
                              + (". Value-added and roaming charges are often unwanted; confirm you signed up."
                                 if risky else "."),
                    "amount_cents": i["amount_cents"],
                })
            elif prev[key] > 0 and (i["amount_cents"] - prev[key]) / prev[key] > 0.20:
                findings.append({
                    "kind": "line_increase", "severity": "info",
                    "title": f"{i['label']} up {(i['amount_cents'] - prev[key]) / prev[key]:.0%}",
                    "detail": f"{fmt(prev[key])} → {fmt(i['amount_cents'])} since the last bill.",
                    "amount_cents": i["amount_cents"] - prev[key],
                })

    if history_totals_cents and len(history_totals_cents) >= 3:
        base = int(statistics.median(history_totals_cents))
        if base and (total - base) / base >= 0.25:
            findings.append({
                "kind": "spike", "severity": "warning",
                "title": f"Total is {(total - base) / base:.0%} above your usual {payee} bill",
                "detail": f"{fmt(total)} vs a typical {fmt(base)}.",
                "amount_cents": total - base,
            })

    recoverable = sum(f["amount_cents"] for f in findings if f["kind"] in {"fee", "arithmetic", "tax"})
    recoverable += sum(f["amount_cents"] for f in findings if f["kind"] == "new_charge" and f["severity"] == "warning")
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    worst = max((severity_rank[f["severity"]] for f in findings), default=0)
    return {
        "computed_total_cents": total,
        "stated_total_cents": stated_total_cents,
        "line_items": line_items,
        "findings": findings,
        "potential_savings_cents": recoverable,
        "status": {3: "dispute", 2: "review", 1: "ok_with_notes", 0: "ok"}[worst],
    }
