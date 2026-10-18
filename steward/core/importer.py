"""Bank-statement CSV importer with column auto-detection.

Handles the common export shapes:
  * one signed Amount column (with or without CR/DR suffixes or parentheses)
  * separate Debit/Withdrawal and Credit/Deposit columns
  * ISO, D/M/Y, M/D/Y and "16 Oct 2026" dates (D/M vs M/D is resolved per file)
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from steward.core.money import to_cents

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000

DATE_HEADERS = ("transaction date", "posted date", "posting date", "value date", "date")
DESC_HEADERS = ("description", "transaction description", "details", "narrative", "merchant", "payee",
                "reference", "particulars", "memo")
AMOUNT_HEADERS = ("amount", "transaction amount", "amount (sgd)")
DEBIT_HEADERS = ("debit", "debit amount", "withdrawal", "withdrawals", "money out", "paid out", "debit (sgd)")
CREDIT_HEADERS = ("credit", "credit amount", "deposit", "deposits", "money in", "paid in", "credit (sgd)")

_TEXT_FORMATS = ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %b %y", "%b %d, %Y", "%Y/%m/%d")


class ImportError_(ValueError):
    """Raised for files we refuse to import (named to avoid shadowing the builtin)."""


@dataclass
class ParsedRow:
    posted_on: date
    description: str
    amount_cents: int
    row_number: int


def _find(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    lowered = [h.strip().lower() for h in headers]
    for cand in candidates:
        if cand in lowered:
            return lowered.index(cand)
    for i, h in enumerate(lowered):
        if any(h.startswith(c) for c in candidates):
            return i
    return None


def parse_amount(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    upper = s.upper()
    if upper.endswith("DR"):
        negative, s = True, s[:-2]
    elif upper.endswith("CR"):
        s = s[:-2]
    s = re.sub(r"[^\d.\-]", "", s)
    if s.startswith("-"):
        negative, s = True, s[1:]
    if not s or s.count(".") > 1 or "-" in s:
        return None
    cents = to_cents(s)
    return -cents if negative else cents


def _dmy_or_mdy(values: list[str]) -> str:
    """Decide D/M/Y vs M/D/Y for slash/dash dates: any first part > 12 means D/M."""
    first, second = 0, 0
    for v in values:
        parts = re.split(r"[/\-.]", v.strip())
        if len(parts) == 3 and len(parts[0]) <= 2:
            try:
                a, b = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            first = max(first, a)
            second = max(second, b)
    if second > 12 and first <= 12:
        return "mdy"
    return "dmy"


def parse_date(raw: str, order: str = "dmy") -> date | None:
    s = raw.strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        d, mo = (a, b) if order == "dmy" else (b, a)
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    for fmt_ in _TEXT_FORMATS:
        try:
            return datetime.strptime(s, fmt_).date()
        except ValueError:
            continue
    return None


def parse_csv(content: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Returns (rows, warnings). Raises ImportError_ for unusable files."""
    if len(content) > MAX_BYTES:
        raise ImportError_("File is larger than 2 MB.")
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    # Skip bank preamble lines until we find a header row with a date column.
    header_idx = None
    for i, row in enumerate(rows[:30]):
        if _find(row, DATE_HEADERS) is not None and (
            _find(row, AMOUNT_HEADERS) is not None or _find(row, DEBIT_HEADERS) is not None
        ):
            header_idx = i
            break
    if header_idx is None:
        raise ImportError_("Could not find a header row with Date and Amount (or Debit/Credit) columns.")
    header = rows[header_idx]
    body = rows[header_idx + 1:]
    if len(body) > MAX_ROWS:
        raise ImportError_(f"Too many rows (max {MAX_ROWS}).")

    i_date = _find(header, DATE_HEADERS)
    i_desc = _find(header, DESC_HEADERS)
    i_amt = _find(header, AMOUNT_HEADERS)
    i_debit = _find(header, DEBIT_HEADERS)
    i_credit = _find(header, CREDIT_HEADERS)
    if i_desc is None:
        raise ImportError_("Could not find a Description column.")

    order = _dmy_or_mdy([r[i_date] for r in body if len(r) > i_date])
    parsed, warnings = [], []
    for n, row in enumerate(body, start=header_idx + 2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            posted = parse_date(row[i_date], order)
            desc = row[i_desc].strip()
            if i_amt is not None and i_debit is None:
                amount = parse_amount(row[i_amt])
            else:
                debit = parse_amount(row[i_debit]) if i_debit is not None and len(row) > i_debit else None
                credit = parse_amount(row[i_credit]) if i_credit is not None and len(row) > i_credit else None
                amount = None
                if debit:
                    amount = -abs(debit)
                elif credit:
                    amount = abs(credit)
        except (IndexError, ValueError):
            warnings.append(f"Row {n}: could not read the row; skipped.")
            continue
        if posted is None or amount is None or not desc:
            warnings.append(f"Row {n}: missing date, amount or description; skipped.")
            continue
        parsed.append(ParsedRow(posted, desc[:255], amount, n))
    return parsed, warnings


def import_hash(user_id: int, account_id: int, row: ParsedRow, occurrence: int) -> str:
    """Stable fingerprint so re-importing the same statement never duplicates rows.
    `occurrence` separates genuinely identical rows (two coffees, same day, same price)."""
    description = re.sub(r"\s+", " ", row.description.upper())
    key = f"{user_id}|{account_id}|{row.posted_on.isoformat()}|{row.amount_cents}|{description}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection when exporting user-controlled text."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value
