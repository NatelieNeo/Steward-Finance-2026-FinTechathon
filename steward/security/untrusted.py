"""Handling of third-party text before it reaches the LLM.

Transaction descriptions, merchant names and bill text are written by whoever controls the
merchant descriptor, so they are untrusted input. Two defences apply:

1. Prompt-injection screening at ingestion: suspicious descriptors are flagged and the
   text is quarantined (never sent to the model; the merchant is shown as quarantined).
2. Data minimisation on every LLM call: PII is redacted, control characters and markup
   are stripped, and fields are length-capped.

Neither is relied on alone: the model cannot move money directly, and every action it
proposes passes the deterministic policy engine (agent.policy).
"""

from __future__ import annotations

import re

INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|all|earlier|system)\b.{0,30}"
        r"\b(instructions?|prompts?|rules?|messages?)\b",
        r"\b(system|developer)\s*(prompt|message|mode)\b",
        r"\byou are now\b",
        r"\b(act|behave) as\b.{0,20}\b(admin|developer|system|dan)\b",
        r"^\s*(system|assistant|user)\s*:",
        r"</?\s*(system|instructions?|tool|assistant)\s*>",
        # An imperative money instruction with an amount ("TRANSFER S$500 TO ..."): bank
        # descriptors name the counterparty, they never instruct the reader.
        r"\b(transfer|send|wire|pay|move)\b\s+(?:(?:s|us)?\$|sgd|usd)\s?\d[\d,.]*.{0,30}\b(to|into)\b",
        r"\b(reveal|print|show|send|leak|output)\b.{0,20}\b(api[_ ]?keys?|passwords?|secrets?|tokens?|prompt)\b",
        r"\bjailbreak\b",
        r"\btool[_ ]?(call|use)\b",
    ]
]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NRIC = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){9,19}(?!\d)")
_ACCOUNT = re.compile(r"\b\d{3}-\d{5,6}-\d{1,3}\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKUP = re.compile(r"[<>`{}\[\]\\]")


def looks_like_injection(text: str) -> bool:
    return any(p.search(text or "") for p in INJECTION_PATTERNS)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact_pii(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _NRIC.sub("[id]", text)
    text = _ACCOUNT.sub("[account]", text)

    def _digits(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[card]"
        return "[number]"

    text = _LONG_DIGITS.sub(_digits, text)
    text = _PHONE.sub("[phone]", text)
    return text


def for_llm(text: str, max_len: int = 80) -> str:
    """Make a third-party string safe to place inside a tool result."""
    if looks_like_injection(text):
        return "[quarantined text]"
    text = _CONTROL.sub(" ", text or "")
    text = _MARKUP.sub("", text)
    text = redact_pii(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]
