"""Liens and tax delinquency on the property record card.

REData has exposed `/parcels/{uuid}/liens/` and `/parcels/{uuid}/tax-payments/`
all along and nothing consumed them (found by auditing REData's api-reference
against this codebase's gateways). For an application about abandoned places
they are the most telling records on the card: an open code-enforcement lien
and years of delinquent tax are what "abandoned" looks like in public records,
long before anything says so in words.

Two contract details drive the shaping, and both are easy to get wrong:

- `delinquent` is the publisher's own determination, *not* derived from `paid`.
  A current bill is unpaid before its due date without being delinquent, so
  counting unpaid rows would overstate distress on a perfectly current
  property.
- `status` on a lien is free text that publishers spell inconsistently, so it
  is shown as a label and never branched on.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.property_records import _lien_rows, _tax_status


class TaxStatusTests(SimpleTestCase):
    def test_unpaid_but_not_delinquent_is_not_distress(self) -> None:
        """The bill simply is not due yet."""
        status = _tax_status([{"tax_year": 2025, "paid": False, "delinquent": False}])

        self.assertEqual(status["delinquent_count"], 0)
        self.assertEqual(status["latest_year"], 2025)

    def test_delinquent_years_are_counted_and_listed(self) -> None:
        rows = [
            {"tax_year": 2023, "delinquent": True},
            {"tax_year": 2024, "delinquent": True},
            {"tax_year": 2022, "paid": True, "delinquent": False},
        ]

        status = _tax_status(rows)

        self.assertEqual(status["delinquent_count"], 2)
        self.assertEqual(status["delinquent_years"], [2023, 2024])
        self.assertEqual(status["latest_year"], 2024)

    def test_rows_without_a_year_are_ignored_rather_than_crashing(self) -> None:
        status = _tax_status([{"delinquent": True}, {"tax_year": None}, "not a dict"])

        self.assertEqual(status["latest_year"], None)
        self.assertEqual(status["delinquent_count"], 0)

    def test_no_rows_is_not_an_error(self) -> None:
        self.assertEqual(_tax_status([])["delinquent_count"], 0)


class LienRowTests(SimpleTestCase):
    def test_newest_filing_first(self) -> None:
        rows = _lien_rows(
            [
                {"lien_type": "tax", "filed_date": "2019-01-01"},
                {"lien_type": "code enforcement", "filed_date": "2024-06-01"},
            ]
        )

        self.assertEqual([row["lien_type"] for row in rows], ["code enforcement", "tax"])

    def test_free_text_status_is_passed_through_untouched(self) -> None:
        """Publishers spell this inconsistently; interpreting it would be a guess."""
        rows = _lien_rows([{"lien_type": "tax", "status": "OPEN - referred", "filed_date": "2024-01-01"}])

        self.assertEqual(rows[0]["status"], "OPEN - referred")

    def test_a_row_without_a_type_still_renders(self) -> None:
        rows = _lien_rows([{"amount": "1200.00", "filed_date": "2024-01-01"}])

        self.assertEqual(rows[0]["lien_type"], "Lien")

    def test_the_list_is_capped(self) -> None:
        """The card is a summary; the full history lives in county records."""
        rows = _lien_rows([{"lien_type": "tax", "filed_date": f"20{n:02d}-01-01"} for n in range(20)])

        self.assertLessEqual(len(rows), 8)

    def test_undated_rows_do_not_crash_the_sort(self) -> None:
        rows = _lien_rows([{"lien_type": "tax"}, {"lien_type": "code", "filed_date": "2024-01-01"}])

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["lien_type"], "code")
