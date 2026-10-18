"""Recurring detection, anomalies, forecasting, budgeting, health score and goals."""

from datetime import date, timedelta

from steward.core import anomalies
from steward.core.budgeting import budget_status, suggest_budgets
from steward.core.forecast import build_forecast, check_affordability
from steward.core.goals import goal_progress, safe_to_save
from steward.core.health import HealthInputs, score
from steward.core.money import add_months
from steward.core.recurring import detect_recurring
from steward.core.types import Txn

TODAY = date(2026, 10, 16)
_id = iter(range(1, 100_000))


def t(days_ago: int, merchant: str, cents: int, category: str = "Other", kind: str = "purchase", desc=None) -> Txn:
    return Txn(id=next(_id), posted_on=TODAY - timedelta(days=days_ago), description=desc or merchant.upper(),
               merchant=merchant, amount_cents=cents, category=category, account_id=1, kind=kind)


def monthly(merchant, cents, category, months=6, day=8, amounts=None, sign=-1):
    out = []
    for i in range(months, 0, -1):
        d = add_months(TODAY.replace(day=day), -i + 1)
        if d > TODAY:
            continue
        amount = amounts[months - i] if amounts else cents
        out.append(Txn(id=next(_id), posted_on=d, description=merchant.upper(), merchant=merchant,
                       amount_cents=sign * amount, category=category, account_id=1))
    return out


class TestRecurring:
    def test_detects_monthly_subscription_and_price_hike(self):
        txns = monthly("Netflix", 1998, "Subscriptions", amounts=[1998, 1998, 1998, 1998, 2298, 2298])
        [s] = detect_recurring(txns, TODAY)
        assert s.cadence == "monthly" and s.kind == "subscription" and s.active
        assert s.previous_amount_cents == 1998 and s.last_amount_cents == 2298
        assert round(s.price_change_pct, 3) == 0.150
        assert s.next_expected_date == date(2026, 11, 8)
        assert s.monthly_equivalent_cents == 2298

    def test_variable_bill_is_not_a_price_hike(self):
        txns = monthly("SP Group", 0, "Utilities", amounts=[13000, 14200, 12900, 13800, 13500, 14100])
        [s] = detect_recurring(txns, TODAY)
        assert s.kind == "bill" and s.price_change_pct is None and not s.fixed_amount

    def test_detects_salary_as_income(self):
        txns = monthly("Salary", 520_000, "Income", day=25, sign=1)
        [s] = detect_recurring(txns, TODAY)
        assert s.kind == "income" and s.direction == "inflow"

    def test_irregular_spending_is_not_recurring(self):
        txns = [t(d, "NTUC FairPrice", -(3000 + d * 137 % 6000), "Groceries") for d in (1, 4, 6, 11, 13, 20, 22, 29, 33)]
        assert detect_recurring(txns, TODAY) == []

    def test_one_late_payment_does_not_hide_a_bill(self):
        dates = [150, 120, 89, 74, 59, 28]  # one payment 15 days late, then back on schedule
        txns = [t(d, "Singtel", -4590, "Utilities") for d in dates]
        [s] = detect_recurring(txns, TODAY)
        assert s.cadence == "monthly"

    def test_lapsed_subscription_is_inactive(self):
        txns = [t(d, "Spotify", -1198, "Subscriptions") for d in (200, 170, 140, 110)]
        [s] = detect_recurring(txns, TODAY)
        assert not s.active

    def test_quarantined_transactions_are_ignored(self):
        txns = [Txn(id=next(_id), posted_on=TODAY - timedelta(days=30 * k), description="X", merchant="Q",
                    amount_cents=-100, flags=("prompt_injection_suspected",)) for k in range(5)]
        assert detect_recurring(txns, TODAY) == []


class TestAnomalies:
    def test_duplicate_charge(self):
        a, b = t(9, "Shopee", -8990, "Shopping"), t(9, "Shopee", -8990, "Shopping")
        [f] = anomalies.find_duplicate_charges([a, b, t(3, "Lazada", -5000, "Shopping")], TODAY)
        assert f.kind == "duplicate_charge" and f.txn_ids == [a.id, b.id]

    def test_habitual_small_purchases_are_not_duplicates(self):
        coffees = [t(d, "Starbucks", -1200, "Dining") for d in range(0, 40, 2) for _ in (0, 1)]
        assert anomalies.find_duplicate_charges(coffees, TODAY) == []

    def test_fees(self):
        txns = [t(5, "FX Fee", -250, "Fees & Charges", desc="FX TRANSACTION FEE"),
                t(40, "Bank", -1000, "Other", desc="LATE PAYMENT FEE"), t(200, "Bank", -999, desc="LATE FEE")]
        [f] = anomalies.find_fees(txns, TODAY)
        assert f.amount_cents == 1250 and f.data["count"] == 2

    def test_outlier_needs_both_z_score_and_scale(self):
        base = [t(d, "Shop", -(3000 + d * 50), "Shopping") for d in range(20, 120, 8)]
        big = t(5, "Apple Store", -184_900, "Shopping")
        found = anomalies.find_outliers(base + [big], TODAY)
        assert [f.txn_ids for f in found] == [[big.id]]

    def test_bill_spike(self):
        txns = monthly("SP Group", 0, "Utilities", amounts=[13000, 13200, 12900, 13100, 13050, 18000])
        series = detect_recurring(txns, TODAY)
        [f] = anomalies.find_bill_spikes(series)
        assert f.kind == "bill_spike" and f.data["pct"] > 0.3

    def test_subscription_overlap(self):
        txns = (monthly("Netflix", 2298, "Subscriptions") + monthly("Disney+", 1198, "Subscriptions", day=19)
                + monthly("Spotify", 1198, "Subscriptions", day=14))
        [f] = anomalies.find_subscription_overlaps(detect_recurring(txns, TODAY))
        assert f.data["group"] == "video streaming" and set(f.data["merchants"]) == {"Netflix", "Disney+"}


class TestForecast:
    def _history(self):
        txns = []
        for d in range(1, 90):
            txns.append(t(d, "Kopitiam", -1000, "Dining"))
        txns += monthly("Salary", 500_000, "Income", day=25, sign=1)
        txns += monthly("Rent", 150_000, "Housing", day=1)
        return txns

    def test_projects_salary_rent_and_daily_spend(self):
        txns = self._history()
        series = detect_recurring(txns, TODAY)
        fc = build_forecast(start_balance_cents=200_000, today=TODAY, txns=txns, series=series, horizon_days=30,
                            floor_cents=50_000)
        assert fc.daily_discretionary_cents == 1000
        assert fc.next_income_date == date(2026, 10, 25)
        by_date = {p.on: p for p in fc.points}
        # 9 days of S$10 spend, then +S$5,000 salary on the 25th.
        assert by_date[date(2026, 10, 25)].expected_cents == 200_000 - 9 * 1000 + 500_000
        assert any(e.label == "Rent" for e in by_date[date(2026, 11, 1)].events)
        assert fc.overdraft_probability < 0.01

    def test_open_bill_replaces_its_recurring_occurrence(self):
        txns = monthly("SP Group", 14000, "Utilities", day=7)
        series = detect_recurring(txns, TODAY)
        fc = build_forecast(start_balance_cents=100_000, today=TODAY, txns=txns, series=series, horizon_days=30,
                            open_bills=[{"due_on": date(2026, 10, 20), "amount_cents": 19000, "label": "SP bill",
                                         "merchant": "SP Group"}])
        events = [e for p in fc.points for e in p.events]
        assert [e.amount_cents for e in events] == [-19000]

    def test_affordability_verdicts(self):
        txns = self._history()
        fc = build_forecast(start_balance_cents=200_000, today=TODAY, txns=txns, series=detect_recurring(txns, TODAY),
                            horizon_days=30, floor_cents=50_000)
        assert check_affordability(fc, 10_000)["verdict"] == "affordable"
        assert check_affordability(fc, 150_000)["verdict"] in {"risky", "not_affordable"}
        assert check_affordability(fc, 500_000)["verdict"] == "not_affordable"

    def test_one_off_purchase_excluded_from_baseline(self):
        txns = [t(d, "Kopitiam", -1000, "Dining") for d in range(1, 85)] + [t(10, "Apple Store", -184_900, "Shopping")]
        fc = build_forecast(start_balance_cents=100_000, today=TODAY, txns=txns, series=[], horizon_days=14)
        assert fc.daily_discretionary_cents == 1000


class TestBudgeting:
    def test_suggestions_use_median_and_trim_wants(self):
        txns = []
        for i, amount in enumerate([80_000, 120_000, 100_000]):
            d = add_months(TODAY.replace(day=10), -(i + 1))
            txns.append(Txn(id=next(_id), posted_on=d, description="X", merchant="Resto", amount_cents=-amount,
                            category="Dining"))
            txns.append(Txn(id=next(_id), posted_on=d, description="SALARY", merchant="Salary", amount_cents=300_000,
                            category="Income"))
        out = suggest_budgets(txns, TODAY)
        [s] = out["suggestions"]
        assert s["basis_cents"] == 100_000
        # wants (S$1,000) exceed 30% of income (S$900): trimmed, but never below 70% of history.
        assert s["suggested_cents"] == 90_000
        assert out["fifty_thirty_twenty"]["wants_pct"] == round(100_000 / 300_000, 4)

    def test_status_projects_flexible_spend_but_not_paid_rent(self):
        txns = [t(15, "Rent", -150_000, "Housing")]  # paid on the 1st
        txns += [t(d, "Kopitiam", -2000, "Dining") for d in range(0, 15)]
        rows = {r["category"]: r for r in budget_status({"Housing": 150_000, "Dining": 50_000}, txns, TODAY,
                                                        recurring_merchants={"Rent"})}
        assert rows["Housing"]["status"] == "on_track" and rows["Housing"]["projected_cents"] == 150_000
        assert rows["Dining"]["spent_cents"] == 30_000
        assert rows["Dining"]["status"] == "at_risk" and rows["Dining"]["projected_cents"] > 50_000

    def test_over_budget(self):
        rows = budget_status({"Shopping": 10_000}, [t(2, "Shopee", -12_000, "Shopping")], TODAY)
        assert rows[0]["status"] == "over"


class TestHealthAndGoals:
    def test_health_score_is_bounded_and_explained(self):
        good = score(HealthInputs(500_000, [300_000] * 3, 200_000, 1_500_000, 0, 0, 4, 4, 0, [80_000] * 3))
        bad = score(HealthInputs(300_000, [330_000] * 3, 200_000, 0, 5000, 4, 4, 0, 600_000, [10_000, 90_000, 30_000]))
        assert 85 <= good["score"] <= 100 and good["grade"] == "A"
        assert 0 <= bad["score"] < 40 and bad["grade"] == "E"
        assert len(bad["focus"]) == 2

    def test_goal_progress(self):
        g = goal_progress("Japan", 300_000, 80_000, date(2027, 3, 31), TODAY, 30_000)
        assert g["remaining_cents"] == 220_000 and g["required_monthly_cents"] > 40_000 and g["on_track"] is False

    def test_safe_to_save_uses_half_the_pessimistic_headroom(self):
        txns = [t(d, "Kopitiam", -1000, "Dining") for d in range(1, 85)]
        fc = build_forecast(start_balance_cents=300_000, today=TODAY, txns=txns, series=[], horizon_days=30,
                            floor_cents=100_000)
        amount = safe_to_save(fc)
        assert amount % 1000 == 0
        assert amount <= (fc.min_low_cents - 100_000) / 2
        broke = build_forecast(start_balance_cents=90_000, today=TODAY, txns=txns, series=[], horizon_days=30,
                               floor_cents=100_000)
        assert safe_to_save(broke) == 0
