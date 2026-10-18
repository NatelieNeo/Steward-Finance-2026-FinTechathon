"""Money handling, merchant normalisation and the categorizer cascade."""

from datetime import date

import pytest

from steward.core.categorizer import NaiveBayes, base_model, categorize
from steward.core.merchant import normalize_merchant
from steward.core.money import add_months, fmt, round_up_to, to_cents


class TestMoney:
    def test_to_cents_rounds_half_up_without_float_error(self):
        assert to_cents("0.1") + to_cents("0.2") == to_cents("0.3") == 30
        assert to_cents("12.345") == 1235
        assert to_cents("-4.995") == -500
        assert to_cents(19.98) == 1998

    @pytest.mark.parametrize("bad", ["abc", "NaN", "inf", ""])
    def test_to_cents_rejects_non_amounts(self, bad):
        with pytest.raises(ValueError):
            to_cents(bad)

    def test_format(self):
        assert fmt(123456) == "S$1,234.56"
        assert fmt(-500) == "-S$5.00"
        assert fmt(100, "USD") == "US$1.00"

    def test_add_months_clamps_to_month_end(self):
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
        assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)

    def test_round_up(self):
        assert round_up_to(12_301, 1000) == 13_000
        assert round_up_to(0, 1000) == 0


class TestMerchant:
    @pytest.mark.parametrize("raw, expected", [
        ("POS 1234 NTUC FAIRPRICE BEDOK SG 12/10", "NTUC FairPrice"),
        ("NETFLIX.COM SINGAPORE", "Netflix"),
        ("GRAB*RIDE A-12345", "Grab"),
        ("GRABFOOD *ORDER SINGAPORE", "GrabFood"),
        ("GIRO RENT ORCHID PROPERTY MGMT", "Rent"),
        ("BILL PAYMENT SP GROUP 2026-10", "SP Group"),
        ("SALARY ACME ANALYTICS PTE LTD GIRO", "Salary"),
        ("VISA DEBIT 4821 TIONG BAHRU BAKERY 0034", "Tiong Bahru Bakery"),
        ("   ", "Unknown Merchant"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_merchant(raw) == expected

    def test_current_is_not_rent(self):
        assert normalize_merchant("CURRENT ACCOUNT FEE") != "Rent"


class TestCategorizer:
    def test_lexicon_merchant(self):
        c = categorize("NETFLIX.COM", "Netflix", -1998)
        assert (c.category, c.source) == ("Subscriptions", "lexicon")

    def test_user_rule_beats_everything(self):
        c = categorize("NETFLIX.COM", "Netflix", -1998, user_rules=[("netflix", "Entertainment")])
        assert (c.category, c.source) == ("Entertainment", "rule")

    def test_memory_beats_lexicon(self):
        c = categorize("SHOPEE SINGAPORE", "Shopee", -1000, memory={"Shopee": "Groceries"})
        assert (c.category, c.source) == ("Groceries", "memory")

    def test_refund_from_shop_is_income(self):
        assert categorize("SHOPEE REFUND", "Shopee", 1500).category == "Income"

    def test_keyword_fee(self):
        assert categorize("ANNUAL CARD FEE", "Annual Card Fee", -19260).category == "Fees & Charges"

    def test_naive_bayes_generalises_to_unseen_descriptors(self):
        c = categorize("MALA XIANG GUO HOTPOT CLARKE QUAY", "Mala Xiang Guo Hotpot", -3200)
        assert c.category == "Dining" and c.source == "model"

    def test_unknown_debit_falls_back_to_other(self):
        c = categorize("QZX 99812", "Qzx", -500)
        assert c.category == "Other" and c.source == "default"

    def test_naive_bayes_probabilities_sum_to_one(self):
        probs = base_model().predict_proba("hotel booking")
        assert abs(sum(probs.values()) - 1) < 1e-9
        assert max(probs, key=probs.get) == "Travel"

    def test_naive_bayes_tiny(self):
        nb = NaiveBayes().fit([("coffee shop", "Dining"), ("taxi ride", "Transport")])
        assert nb.predict("morning coffee")[0] == "Dining"
        assert nb.predict("zzz") == (None, 0.0)
