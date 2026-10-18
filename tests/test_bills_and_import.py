"""Bill parsing/auditing and the CSV statement importer."""

from datetime import date

import pytest

from steward.core.bill_audit import audit_bill, parse_bill_text
from steward.core.importer import ImportError_, csv_safe, import_hash, parse_amount, parse_csv, parse_date

BILL = """Singtel Mobile - Statement Oct 2026
5G SIM Only 100GB plan            42.11
Premium SMS service (VAS)          4.91
Late payment charge               10.00
Loyalty discount                  (2.00)
GST 9%                             4.99
Sub-total                         60.01
Total amount due              S$ 60.01"""


class TestBillAudit:
    def test_parse(self):
        p = parse_bill_text(BILL)
        assert p["stated_total_cents"] == 6001
        types = {i["label"]: i["type"] for i in p["line_items"]}
        assert types["Late payment charge"] == "fee"
        assert types["GST 9%"] == "tax"
        assert types["Loyalty discount"] == "discount"
        assert sum(i["amount_cents"] for i in p["line_items"]) == 6001

    def test_clean_bill(self):
        items = [{"label": "Fibre broadband", "amount_cents": 3661, "type": "charge"},
                 {"label": "GST 9%", "amount_cents": 329, "type": "tax"}]
        r = audit_bill(items, 3990, previous_items=items, history_totals_cents=[3990] * 4)
        assert r["status"] == "ok" and r["findings"] == []

    def test_findings(self):
        p = parse_bill_text(BILL)
        prev = [{"label": "5G SIM Only 100GB plan", "amount_cents": 4211, "type": "charge"}]
        r = audit_bill(p["line_items"], p["stated_total_cents"], prev, [4590, 4590, 4590], "Singtel")
        kinds = sorted(f["kind"] for f in r["findings"])
        assert kinds == ["fee", "new_charge", "spike"]
        new = next(f for f in r["findings"] if f["kind"] == "new_charge")
        assert new["severity"] == "warning"  # VAS is a classic unwanted add-on
        assert r["status"] == "review"
        assert r["potential_savings_cents"] == 1000 + 491

    def test_arithmetic_and_gst_errors(self):
        items = [{"label": "Plan", "amount_cents": 10000, "type": "charge"},
                 {"label": "GST", "amount_cents": 1000, "type": "tax"}]  # should be 900
        r = audit_bill(items, stated_total_cents=12000)
        kinds = {f["kind"] for f in r["findings"]}
        assert kinds == {"arithmetic", "tax"} and r["status"] == "dispute"

    def test_usage_line_matches_across_months(self):
        prev = [{"label": "Electricity usage 330 kWh @ S$0.2994/kWh", "amount_cents": 9880, "type": "charge"}]
        cur = [{"label": "Electricity usage 455 kWh @ S$0.2994/kWh", "amount_cents": 13623, "type": "charge"}]
        [f] = audit_bill(cur, previous_items=prev)["findings"]
        assert f["kind"] == "line_increase"


class TestImporter:
    def test_signed_amount_iso_dates(self):
        rows, warnings = parse_csv(b"Date,Description,Amount\n2026-10-01,NETFLIX.COM,-19.98\n2026-10-02,SALARY,5200\n")
        assert [(r.posted_on, r.amount_cents) for r in rows] == [(date(2026, 10, 1), -1998), (date(2026, 10, 2), 520000)]
        assert warnings == []

    def test_debit_credit_columns_with_preamble_and_dmy(self):
        content = ("Account Statement\nAccount: 123-45678-9\n\n"
                   "Transaction Date,Value Date,Details,Withdrawals,Deposits\n"
                   "13/10/2026,13/10/2026,GRAB*RIDE,\"1,234.50\",\n"
                   "14/10/2026,14/10/2026,REFUND SHOPEE,,12.00\n").encode()
        rows, _ = parse_csv(content)
        assert [(r.posted_on, r.amount_cents) for r in rows] == [(date(2026, 10, 13), -123450), (date(2026, 10, 14), 1200)]

    def test_mdy_detected(self):
        rows, _ = parse_csv(b"Date,Description,Amount\n10/13/2026,A,-1.00\n10/14/2026,B,-2.00\n")
        assert rows[0].posted_on == date(2026, 10, 13)

    @pytest.mark.parametrize("raw, cents", [("(12.30)", -1230), ("12.30 DR", -1230), ("12.30CR", 1230),
                                            ("S$1,000.00", 100000), ("-0.5", -50), ("", None), ("1.2.3", None)])
    def test_amounts(self, raw, cents):
        assert parse_amount(raw) == cents

    def test_text_dates(self):
        assert parse_date("16 Oct 2026") == date(2026, 10, 16)
        assert parse_date("31/02/2026") is None

    def test_bad_rows_are_reported_not_fatal(self):
        rows, warnings = parse_csv(b"Date,Description,Amount\nnot-a-date,X,-1\n2026-10-01,Y,abc\n2026-10-01,Z,-3\n")
        assert len(rows) == 1 and len(warnings) == 2

    def test_rejects_files_without_header(self):
        with pytest.raises(ImportError_):
            parse_csv(b"hello,world\n1,2\n")

    def test_rejects_oversized_files(self):
        with pytest.raises(ImportError_):
            parse_csv(b"Date,Description,Amount\n" + b"2026-10-01,X,-1\n" * 200_000)

    def test_import_hash_is_stable_and_separates_repeats(self):
        rows, _ = parse_csv(b"Date,Description,Amount\n2026-10-01,KOPI,-1.80\n2026-10-01,KOPI,-1.80\n")
        h1, h2 = import_hash(1, 1, rows[0], 1), import_hash(1, 1, rows[1], 2)
        assert h1 != h2 and h1 == import_hash(1, 1, rows[1], 1)

    @pytest.mark.parametrize("value", ["=HYPERLINK(1)", "+1", "-1+2", "@SUM(A1)"])
    def test_csv_export_neutralises_formulas(self, value):
        assert csv_safe(value).startswith("'")
