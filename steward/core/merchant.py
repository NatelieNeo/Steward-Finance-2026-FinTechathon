"""Turn raw bank descriptors ("POS 1234 NTUC FAIRPRICE BEDOK SG 12/10") into merchant names."""

from __future__ import annotations

import re

# Canonical names for merchants whose descriptors vary. Checked against the cleaned,
# upper-cased descriptor; first match wins, so specific patterns come first.
ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), name)
    for p, name in [
        (r"GRAB ?FOOD|GRAB\*? ?FOOD", "GrabFood"),
        (r"\bGRAB\b", "Grab"),
        (r"GOJEK", "Gojek"),
        (r"FOODPANDA", "foodpanda"),
        (r"DELIVEROO", "Deliveroo"),
        (r"NETFLIX", "Netflix"),
        (r"SPOTIFY", "Spotify"),
        (r"DISNEY", "Disney+"),
        (r"YOUTUBE", "YouTube Premium"),
        (r"ICLOUD|APPLE\.COM/BILL|APPLE COM BILL", "Apple iCloud"),
        (r"APPLE STORE|APPLE ORCHARD", "Apple Store"),
        (r"AMAZON PRIME|PRIME VIDEO", "Amazon Prime"),
        (r"AMAZON|AMZN", "Amazon"),
        (r"NTUC|FAIRPRICE", "NTUC FairPrice"),
        (r"COLD STORAGE", "Cold Storage"),
        (r"SHENG SIONG", "Sheng Siong"),
        (r"GIANT", "Giant"),
        (r"SIMPLYGO|TRANSITLINK|BUS/MRT|BUS MRT|EZ-?LINK", "SimplyGo Transit"),
        (r"\bSMRT\b", "SMRT"),
        (r"COMFORTDELGRO|CDG", "ComfortDelGro"),
        (r"SHOPEE", "Shopee"),
        (r"LAZADA", "Lazada"),
        (r"UNIQLO", "Uniqlo"),
        (r"SP GROUP|SP SERVICES|SPSERVICES", "SP Group"),
        (r"SINGTEL", "Singtel"),
        (r"STARHUB", "StarHub Broadband"),
        (r"\bM1\b", "M1"),
        (r"ANYTIME FITNESS", "Anytime Fitness"),
        (r"GOLDEN VILLAGE|\bGV\b", "Golden Village"),
        (r"GUARDIAN", "Guardian"),
        (r"WATSONS", "Watsons"),
        (r"KOPITIAM", "Kopitiam"),
        (r"KOUFU", "Koufu"),
        (r"FOOD REPUBLIC", "Food Republic"),
        (r"YA KUN", "Ya Kun Kaya Toast"),
        (r"STARBUCKS", "Starbucks"),
        (r"TOAST BOX", "Toast Box"),
        (r"MCDONALD", "McDonald's"),
        (r"SUSHI TEI", "Sushi Tei"),
        (r"DIN TAI FUNG", "Din Tai Fung"),
        (r"PARADISE DYNASTY", "Paradise Dynasty"),
        (r"SALARY|PAYROLL", "Salary"),
        (r"INTEREST (EARNED|CREDIT)|INTEREST PAID", "Bank Interest"),
        (r"INSURANCE|PRUDENTIAL|\bAIA\b|INCOME INSURANCE|GREAT EASTERN", "Insurance Premium"),
        (r"PROPERTY MGMT|\bRENT\b", "Rent"),
        (r"TRANSFER TO SAVINGS|TO SAVINGS", "Savings Transfer"),
        (r"TRANSFER FROM CHECKING|FROM CHECKING", "Savings Transfer"),
        (r"LATE PAYMENT|LATE FEE|LATE CHARGE", "Late Payment Charge"),
        (r"FX (TRANSACTION )?FEE|FOREIGN (TRANSACTION|CURRENCY) FEE", "FX Fee"),
        (r"PROVISIONAL CREDIT", "Provisional Credit"),
        (r"AGODA|BOOKING\.COM|EXPEDIA|SCOOT|SINGAPORE AIRLINES|JETSTAR", "Travel Booking"),
    ]
]

_LEADING_NOISE = re.compile(
    r"^(POS|NETS|EFTPOS|VISA|MASTERCARD|MC|DEBIT|CREDIT|PURCHASE|CARD|PAYNOW|GIRO|IBG|FAST|"
    r"TRF|TRANSFER|BILL PAYMENT|BILL PMT|ONLINE|PAYMENT TO|PMT|SALE|DD|SQ \*|SQ\*|PAYPAL \*|PAYPAL\*)\b[\s\-:*]*"
)
_DROP_TOKENS = {
    "SINGAPORE", "SG", "SGP", "SIN", "PTE", "LTD", "LIMITED", "CO", "INC", "LLC", "COM", "WWW",
    "SGD", "USD", "THE", "BHD", "SDN", "SINGAP",
}


def clean_descriptor(description: str) -> str:
    text = description.upper()
    text = re.sub(r"[^A-Z0-9&'*/.\- ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        stripped = _LEADING_NOISE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def normalize_merchant(description: str) -> str:
    """Best-effort canonical merchant name for a raw descriptor."""
    cleaned = clean_descriptor(description)
    for pattern, name in ALIASES:
        if pattern.search(cleaned):
            return name
    text = cleaned.replace("*", " ").replace(".COM", " ").replace(".SG", " ")
    tokens = []
    for tok in re.split(r"[\s/\-]+", text):
        tok = tok.strip(".'&")
        if not tok or tok in _DROP_TOKENS:
            continue
        if any(ch.isdigit() for ch in tok):
            continue  # store numbers, references, dates
        if len(tok) == 1:
            continue
        tokens.append(tok)
        if len(tokens) == 4:
            break
    if not tokens:
        return "Unknown Merchant"
    return " ".join(t.capitalize() if len(t) > 3 else t for t in tokens)
