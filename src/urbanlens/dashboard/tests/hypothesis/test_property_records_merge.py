"""Tests for per-field merging of multiple tiers' PropertyRecord results.

Covers docs/property-records-plan.md section 4: lower tier number wins per
field, a single-tier input passes through unchanged, and disagreements
between tiers are flagged in field_mismatches rather than silently resolved.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.property_records.merge import merge_records
from urbanlens.dashboard.services.apis.property_records.schema import PropertyRecord, RecordSource


def _record(tier: int, **overrides) -> PropertyRecord:
    defaults = {
        "situs_address": "",
        "county": "Albany County",
        "state": "NY",
        "fips": "36001",
        "source": RecordSource(tier=tier, provider=f"Tier {tier} provider"),
        "confidence": 1.0 / tier,
    }
    defaults.update(overrides)
    return PropertyRecord(**defaults)


class MergeRecordsTests(TestCase):
    def test_single_record_passes_through_unchanged(self) -> None:
        record = _record(1, situs_address="123 Main St")
        merged = merge_records([record])
        self.assertIs(merged, record)

    def test_lower_tier_wins_when_both_have_the_same_field(self) -> None:
        tier1 = _record(1, situs_address="123 Main St")
        tier2 = _record(2, situs_address="456 Other Ave")
        merged = merge_records([tier1, tier2])
        self.assertEqual(merged.situs_address, "123 Main St")
        self.assertEqual(merged.field_sources["situs_address"], 1)

    def test_order_of_input_list_does_not_matter(self) -> None:
        tier1 = _record(1, situs_address="123 Main St")
        tier2 = _record(2, situs_address="456 Other Ave")
        merged = merge_records([tier2, tier1])
        self.assertEqual(merged.situs_address, "123 Main St")

    def test_higher_tier_fills_a_field_the_lower_tier_left_blank(self) -> None:
        tier1 = _record(1, situs_address="123 Main St")
        tier2 = _record(2, land_use_code="RES")
        merged = merge_records([tier1, tier2])
        self.assertEqual(merged.situs_address, "123 Main St")
        self.assertEqual(merged.land_use_code, "RES")
        self.assertEqual(merged.field_sources["land_use_code"], 2)
        self.assertEqual(merged.field_sources["situs_address"], 1)

    def test_agreeing_tiers_are_not_flagged_as_a_mismatch(self) -> None:
        tier1 = _record(1, apn="1-2-3")
        tier2 = _record(2, apn="1-2-3")
        merged = merge_records([tier1, tier2])
        self.assertEqual(merged.field_mismatches, ())

    def test_disagreeing_tiers_are_flagged_as_a_mismatch(self) -> None:
        tier1 = _record(1, apn="1-2-3")
        tier2 = _record(2, apn="9-9-9")
        merged = merge_records([tier1, tier2])
        self.assertIn("apn", merged.field_mismatches)
        # The lower tier still wins the actual value despite the mismatch flag.
        self.assertEqual(merged.apn, "1-2-3")

    def test_case_and_whitespace_differences_are_not_a_mismatch(self) -> None:
        """A GIS layer's '123 MAIN ST' vs a scraped page's '123 Main St' is formatting, not disagreement."""
        tier1 = _record(1, situs_address="123  MAIN ST")
        tier3 = _record(3, situs_address="123 Main St")
        merged = merge_records([tier1, tier3])
        self.assertEqual(merged.field_mismatches, ())
        self.assertEqual(merged.situs_address, "123  MAIN ST")

    def test_owner_name_tuples_are_compared_normalized(self) -> None:
        tier1 = _record(1, owner_name=("JANE SMITH",))
        tier3 = _record(3, owner_name=("Jane Smith",))
        merged = merge_records([tier1, tier3])
        self.assertEqual(merged.field_mismatches, ())

    def test_zero_value_counts_as_present_not_missing(self) -> None:
        tier1 = _record(1, market_value=0.0)
        tier2 = _record(2, market_value=500000.0)
        merged = merge_records([tier1, tier2])
        self.assertEqual(merged.market_value, 0.0)
        self.assertEqual(merged.field_sources["market_value"], 1)

    def test_record_level_metadata_is_not_diffed_as_a_field(self) -> None:
        tier1 = _record(1)
        tier2 = _record(2)
        merged = merge_records([tier1, tier2])
        self.assertNotIn("source", merged.field_sources)
        self.assertNotIn("confidence", merged.field_sources)
        self.assertNotIn("county", merged.field_mismatches)

    def test_three_tiers_merge_correctly(self) -> None:
        tier1 = _record(1, situs_address="123 Main St")
        tier2 = _record(2, apn="1-2-3")
        tier3 = _record(3, land_use_code="RES")
        merged = merge_records([tier1, tier2, tier3])
        self.assertEqual(merged.situs_address, "123 Main St")
        self.assertEqual(merged.apn, "1-2-3")
        self.assertEqual(merged.land_use_code, "RES")
        self.assertEqual(merged.field_sources, {"situs_address": 1, "apn": 2, "land_use_code": 3})

    def test_merged_records_own_source_and_confidence_come_from_the_primary_tier(self) -> None:
        tier1 = _record(1, situs_address="123 Main St")
        tier2 = _record(2, apn="1-2-3")
        merged = merge_records([tier1, tier2])
        self.assertEqual(merged.source.tier, 1)
        self.assertEqual(merged.confidence, tier1.confidence)
