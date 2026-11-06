"""Unit tests that run without the (non-redistributable) Dupaco snapshots.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "submission"))

from contract import Page, Snapshot  # noqa: E402
from knowledge.parse import expired_on, parse_page  # noqa: E402
from knowledge.query import parse_question  # noqa: E402

import solution  # noqa: E402

RATES_V1 = """# Rates

Certificates

Regular Certificates

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 12 Month | 2.88% | 2.90%1 | $1,000.00 |
| 6 Month | 3.37% | 3.40%1 | $1,000.00 |

Rates are effective as of 05/01/2026 and are subject to change without notice.

Share Savings

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| Share Savings | $25,000+ | 0.20% | 0.20% |
| Share Savings | $10,000 – $24,999.99 | 0.15% | 0.15% |
| Share Savings | $0 – $9,999.99 | 0.10% | 0.10% |

Rates are effective as of 05/01/2026 and are subject to change without notice.

Auto Loans

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 5.39% APR |
| 2021-23 Model Year | 60 Months | 5.64% APR |

Rates are effective as of 08/31/2026.
"""

RATES_V2 = RATES_V1.replace("| 12 Month | 2.88% | 2.90%1 |", "| 12 Month | 3.37% | 3.40%1 |")

CALCULATOR = """# Vehicle Affordability Calculator

Auto Loan Rates

| Loan Program | Term | Annual Percentage Rate |
| --- | --- | --- |
| 2023–26 Model Year | 60 Months | 5.34% APR |

Rates effective as of 1/1/2026.
"""

NEWS_OLD = """[Dupaco News](https://example.test/newsroom)

March 01, 2024

# Credit union opens new branch

The credit union opened a branch today.

ABOUT THE CREDIT UNION:

The credit union has 24 branch office locations across three states.
"""

NEWS_NEW = NEWS_OLD.replace("March 01, 2024", "May 05, 2026").replace("24 branch", "22 branch").replace(
    "opens new branch", "renovates branch").replace("opened a branch", "renovated a branch")


NEWS_MID = NEWS_OLD.replace("March 01, 2024", "June 01, 2025").replace("24 branch", "23 branch").replace(
    "opens new branch", "hosts open house").replace("opened a branch", "hosted an open house")


def snapshot(sid, day, pages):
    return Snapshot(sid, day, [Page(url=u, title=t, markdown=m, fetched_at=day + "T00:00:00Z") for u, t, m in pages])


def build(*snaps):
    ks = solution.build()
    for s in snaps:
        ks.ingest(s)
    return ks


class ParseTests(unittest.TestCase):
    def test_table_caption_effective_date_and_footnotes(self):
        page = parse_page(RATES_V1, "Rates")
        first = page.rows[0]
        self.assertIn("Regular Certificates", first.caption)
        self.assertEqual(str(first.effective), "2026-05-01")
        self.assertEqual(first.value_cells()[1], ("Annual Percentage Yield (APY)", "2.90%"))  # footnote "1" stripped
        self.assertEqual(first.label_cells(), ["12 Month"])

    def test_key_value_lines_become_rows(self):
        page = parse_page("# Fees\n\nLost debit card replacement fee: $2.50\n\nDomestic wire $25.00 each\n", "Fees")
        self.assertEqual([r.cells for r in page.rows],
                         [["Lost debit card replacement fee", "$2.50"], ["Domestic wire", "$25.00 each"]])

    def test_expired_promotion(self):
        self.assertEqual(str(expired_on("Summer special: 2.95% (offer ended 2025-09-30)")), "2025-09-30")
        self.assertIsNone(expired_on("A great special for summer"))

    def test_question_qualifiers(self):
        q = parse_question("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertEqual((q.intent, q.months, q.model_year), ("rate", {60}, 2025))
        q = parse_question("Share Savings dividend for a balance between $10,000 and $24,999?")
        self.assertEqual(q.amount, 10000)
        q = parse_question("Five-year CD rate?")
        self.assertEqual(q.years, {5})


class AnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s1 = snapshot("01_2026-05-12_base", "2026-05-12", [
            ("https://example.test/rates", "Rates", RATES_V1),
            ("https://example.test/calculators/vehicle-affordability", "Calculator", CALCULATOR),
            ("https://example.test/newsroom/opens-branch", "Opens branch", NEWS_OLD),
        ])
        s2 = snapshot("02_2026-09-28_latest", "2026-09-28", [
            ("https://example.test/rates", "Rates", RATES_V2),
            ("https://example.test/calculators/vehicle-affordability", "Calculator", CALCULATOR),
            ("https://example.test/newsroom/opens-branch", "Opens branch", NEWS_OLD),
            ("https://example.test/newsroom/open-house", "Open house", NEWS_MID),
            ("https://example.test/newsroom/renovates-branch", "Renovates branch", NEWS_NEW),
        ])
        cls.ks = build(s1, s2)

    def test_stale_calculator_loses_to_rate_sheet(self):
        a = self.ks.answer("What's the APR for a 60-month auto loan on a 2025 model-year vehicle?")
        self.assertIn("5.39%", a.text)
        self.assertNotIn("5.34", a.text)
        self.assertEqual(a.sources[0].url, "https://example.test/rates")

    def test_balance_tier(self):
        a = self.ks.answer("What dividend does Share Savings pay on $12,000?")
        self.assertIn("0.15%", a.text)
        self.assertNotIn("0.20%", a.text)

    def test_as_of_uses_the_older_snapshot(self):
        self.assertIn("2.90%", self.ks.answer("What is the 12 month certificate APY?", as_of="2026-05-31").text)
        now = self.ks.answer("What is the 12 month certificate APY?").text
        self.assertIn("3.40%", now)
        self.assertNotIn("2.90%", now)

    def test_change_question_shows_history(self):
        a = self.ks.answer("Did the 12 month certificate rate change?")
        self.assertIn("2.90%", a.text)
        self.assertIn("3.40%", a.text)

    def test_unknown_product_is_refused(self):
        a = self.ks.answer("What is the rate on a construction loan?")
        self.assertFalse(a.confident)

    def test_newest_press_release_wins(self):
        a = self.ks.answer("How many branches does the credit union have?")
        self.assertIn("22", a.text)
        self.assertNotIn("24", a.text)
        self.assertNotIn("23", a.text)

    def test_answers_are_deterministic(self):
        q = "What's the APR for a 60-month auto loan on a 2025 model-year vehicle?"
        self.assertEqual(self.ks.answer(q).text, self.ks.answer(q).text)

    def test_conflicts_report_winner_and_reason(self):
        cs = self.ks.conflicts()
        auto = [c for c in cs if "5.34%" in c.values]
        self.assertTrue(auto)
        self.assertIn("5.39", auto[0].resolution)
        changed = [c for c in cs if "2.90%" in c.values and "3.40%" in c.values]
        self.assertTrue(changed)
        branches = [c for c in cs if "24" in c.values and "22" in c.values]
        self.assertEqual(branches[0].resolution, "22")


class HarnessSelftest(unittest.TestCase):
    """The tiny example site that ships with the harness (a different credit union)."""

    def test_selftest_site(self):
        sys.path.insert(0, str(ROOT / "harness"))
        from run_eval import load_snapshots
        ks = build(*load_snapshots(ROOT / "harness" / "selftest" / "snapshots"))
        self.assertIn("3.50%", ks.answer("Current APY on a six month share certificate?").text)
        self.assertIn("2.95%", ks.answer("What was the 6-month certificate APY in January 2026?", "2026-01-31").text)
        self.assertIn("$2.50", ks.answer("How much is a lost debit card replacement?").text)
        self.assertFalse(ks.answer("What is the 60-month jumbo certificate rate?").confident)


if __name__ == "__main__":
    unittest.main()
