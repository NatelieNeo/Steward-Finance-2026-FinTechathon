"""Hybrid transaction categorizer.

Cascade (first confident answer wins):
  1. user rules          - "anything containing X is Y" (substring, never regex: no ReDoS)
  2. merchant memory     - categories the user corrected before, keyed by merchant
  3. merchant lexicon    - curated canonical merchants and keyword rules
  4. Naive Bayes model   - token/bigram model trained on a labelled seed corpus
  5. "Other" (or Income for unexplained credits)
An LLM batch stage for low-confidence items would slot in before step 5 (roadmap).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache

from steward.core.categories import CATEGORIES
from steward.core.training_data import LABELLED_DESCRIPTIONS

MERCHANT_CATEGORY = {
    "GrabFood": "Dining", "foodpanda": "Dining", "Deliveroo": "Dining", "Kopitiam": "Dining",
    "Koufu": "Dining", "Food Republic": "Dining", "Ya Kun Kaya Toast": "Dining", "Starbucks": "Dining",
    "Toast Box": "Dining", "McDonald's": "Dining", "Sushi Tei": "Dining", "Din Tai Fung": "Dining",
    "Paradise Dynasty": "Dining",
    "Grab": "Transport", "Gojek": "Transport", "SimplyGo Transit": "Transport", "SMRT": "Transport",
    "ComfortDelGro": "Transport",
    "NTUC FairPrice": "Groceries", "Cold Storage": "Groceries", "Sheng Siong": "Groceries", "Giant": "Groceries",
    "Netflix": "Subscriptions", "Spotify": "Subscriptions", "Disney+": "Subscriptions",
    "YouTube Premium": "Subscriptions", "Apple iCloud": "Subscriptions", "Amazon Prime": "Subscriptions",
    "Shopee": "Shopping", "Lazada": "Shopping", "Uniqlo": "Shopping", "Amazon": "Shopping", "Apple Store": "Shopping",
    "SP Group": "Utilities", "Singtel": "Utilities", "StarHub Broadband": "Utilities", "M1": "Utilities",
    "Anytime Fitness": "Health", "Guardian": "Health", "Watsons": "Personal Care",
    "Golden Village": "Entertainment",
    "Salary": "Income", "Bank Interest": "Income", "Provisional Credit": "Income",
    "Insurance Premium": "Insurance", "Rent": "Housing",
    "Savings Transfer": "Transfers",
    "Late Payment Charge": "Fees & Charges", "FX Fee": "Fees & Charges",
    "Travel Booking": "Travel",
}

KEYWORD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), c)
    for p, c in [
        (r"\b(late (payment )?(fee|charge)|finance charge|interest charged|overdraft|fx (transaction )?fee|"
         r"foreign (transaction|currency) fee|annual fee|service charge|atm (fee|charge)|admin fee|penalty)\b",
         "Fees & Charges"),
        (r"\b(salary|payroll|bonus|dividend|interest (earned|credit))\b", "Income"),
        (r"\b(refund|reversal|cashback)\b", "Income"),
        (r"\b(transfer to savings|to savings|own account transfer)\b", "Transfers"),
        (r"\b(rent|property mgmt|landlord|hdb)\b", "Housing"),
        (r"\b(insurance|premium)\b", "Insurance"),
        (r"\b(tuition|course|udemy|coursera|university|school fees?)\b", "Education"),
        (r"\b(clinic|hospital|pharmacy|dental|medical|polyclinic)\b", "Health"),
        (r"\b(hotel|airline|airways|airbnb|agoda|booking\.com|expedia)\b", "Travel"),
        (r"\b(donation|charity|red packet|gift)\b", "Gifts & Donations"),
        (r"\b(loan repayment|card payment|credit card payment)\b", "Debt Payments"),
    ]
]


@dataclass(frozen=True)
class Categorization:
    category: str
    source: str  # rule | memory | lexicon | model | llm | default | user
    confidence: float


_TOKEN = re.compile(r"[a-z][a-z&'+]{1,}")


def tokenize(text: str) -> list[str]:
    words = [w for w in _TOKEN.findall(text.lower()) if len(w) > 1]
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]


class NaiveBayes:
    """Multinomial Naive Bayes with Laplace smoothing. Small, explainable, dependency-free."""

    def __init__(self, alpha: float = 1.0) -> None:
        self.alpha = alpha
        self.class_counts: Counter[str] = Counter()
        self.token_counts: dict[str, Counter[str]] = defaultdict(Counter)
        self.class_totals: Counter[str] = Counter()
        self.vocab: set[str] = set()

    def fit(self, docs: list[tuple[str, str]]) -> NaiveBayes:
        for text, label in docs:
            self.class_counts[label] += 1
            toks = tokenize(text)
            self.token_counts[label].update(toks)
            self.class_totals[label] += len(toks)
            self.vocab.update(toks)
        return self

    def predict_proba(self, text: str) -> dict[str, float]:
        toks = [t for t in tokenize(text) if t in self.vocab]
        if not toks or not self.class_counts:
            return {}
        n_docs = sum(self.class_counts.values())
        v = len(self.vocab)
        log_scores = {}
        for label, count in self.class_counts.items():
            score = math.log(count / n_docs)
            denom = self.class_totals[label] + self.alpha * v
            for tok in toks:
                score += math.log((self.token_counts[label][tok] + self.alpha) / denom)
            log_scores[label] = score
        top = max(log_scores.values())
        exp = {k: math.exp(s - top) for k, s in log_scores.items()}
        total = sum(exp.values())
        return {k: e / total for k, e in exp.items()}

    def predict(self, text: str) -> tuple[str | None, float]:
        probs = self.predict_proba(text)
        if not probs:
            return None, 0.0
        label = max(probs, key=probs.get)
        return label, probs[label]


@lru_cache(maxsize=1)
def base_model() -> NaiveBayes:
    return NaiveBayes().fit(LABELLED_DESCRIPTIONS)


MODEL_THRESHOLD = 0.55


def categorize(
    description: str,
    merchant: str,
    amount_cents: int,
    *,
    user_rules: list[tuple[str, str]] | None = None,
    memory: dict[str, str] | None = None,
) -> Categorization:
    text = f"{description} {merchant}"
    lowered = text.lower()

    for needle, category in user_rules or []:
        if needle and needle.lower() in lowered and category in CATEGORIES:
            return Categorization(category, "rule", 1.0)

    if memory and merchant in memory:
        return Categorization(memory[merchant], "memory", 0.99)

    if merchant in MERCHANT_CATEGORY:
        category = MERCHANT_CATEGORY[merchant]
        # A positive amount from a shop is a refund, not income from that shop's category.
        if amount_cents > 0 and category not in {"Income", "Transfers"}:
            return Categorization("Income", "lexicon", 0.8)
        return Categorization(category, "lexicon", 0.95)

    for pattern, category in KEYWORD_RULES:
        if pattern.search(text):
            return Categorization(category, "lexicon", 0.9)

    label, confidence = base_model().predict(text)
    if label and confidence >= MODEL_THRESHOLD:
        if amount_cents > 0 and label not in {"Income", "Transfers"}:
            return Categorization("Income", "model", confidence * 0.8)
        return Categorization(label, "model", round(confidence, 3))

    if amount_cents > 0:
        return Categorization("Income", "default", 0.5)
    return Categorization("Other", "default", round(confidence, 3) if label else 0.0)
