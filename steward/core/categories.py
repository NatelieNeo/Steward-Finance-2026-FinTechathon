"""Spending taxonomy and how each category is treated by the analytics."""

from __future__ import annotations

CATEGORIES = [
    "Income",
    "Housing",
    "Utilities",
    "Groceries",
    "Dining",
    "Transport",
    "Shopping",
    "Subscriptions",
    "Entertainment",
    "Health",
    "Insurance",
    "Education",
    "Travel",
    "Personal Care",
    "Gifts & Donations",
    "Fees & Charges",
    "Debt Payments",
    "Transfers",
    "Savings",
    "Other",
]

# 50/30/20 buckets.
NEEDS = {"Housing", "Utilities", "Groceries", "Transport", "Insurance", "Health", "Education", "Debt Payments"}
WANTS = {"Dining", "Shopping", "Subscriptions", "Entertainment", "Travel", "Personal Care", "Gifts & Donations", "Other"}
# Money that moves between the user's own pockets, or arrives: never "spending".
NON_SPENDING = {"Income", "Transfers", "Savings"}
FEES = "Fees & Charges"
BILL_CATEGORIES = {"Housing", "Utilities", "Insurance", "Debt Payments", "Education"}
SUBSCRIPTION_CATEGORIES = {"Subscriptions", "Entertainment"}

BUDGETABLE = [c for c in CATEGORIES if c not in NON_SPENDING]


def is_spending(category: str, amount_cents: int) -> bool:
    return amount_cents < 0 and category not in NON_SPENDING


def bucket(category: str) -> str:
    if category in NEEDS:
        return "needs"
    if category == FEES:
        return "fees"
    if category in NON_SPENDING:
        return "non_spending"
    return "wants"
