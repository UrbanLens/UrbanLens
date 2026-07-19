"""Tests for the pure property-record discovery heuristics in ``relevance.py``.

No I/O anywhere in the module under test, so every case here is a plain
input/output check - no mocking, no database.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records import relevance


class TitleRankTests(SimpleTestCase):
    def test_canonical_title_is_tier_zero(self) -> None:
        self.assertEqual(relevance.title_rank("Parcels"), (0, False))

    def test_canonical_title_is_case_and_separator_insensitive(self) -> None:
        self.assertEqual(relevance.title_rank("TAX_PARCELS"), (0, False))

    def test_loose_parcel_like_title_is_tier_one(self) -> None:
        self.assertEqual(relevance.title_rank("Mineral_Parcels"), (1, False))

    def test_unrelated_title_is_tier_two(self) -> None:
        self.assertEqual(relevance.title_rank("Mine_Waste_WFL1"), (2, False))

    def test_stale_marker_is_flagged_but_not_rejected(self) -> None:
        tier, is_stale = relevance.title_rank("Previous Parcels")
        self.assertEqual(tier, 1)
        self.assertTrue(is_stale)


class MatchingFieldCountTests(SimpleTestCase):
    def test_none_fields_counts_zero(self) -> None:
        self.assertEqual(relevance.matching_field_count(None), 0)

    def test_empty_fields_counts_zero(self) -> None:
        self.assertEqual(relevance.matching_field_count([]), 0)

    def test_non_string_entries_are_ignored(self) -> None:
        self.assertEqual(relevance.matching_field_count([123, None, {"name": "OWNER"}]), 0)

    def test_known_parcel_fields_are_counted(self) -> None:
        self.assertGreaterEqual(relevance.matching_field_count(["OWNER_NAME", "PARCEL_ID", "UNRELATED_XYZ"]), 2)


class LooksLikeParcelDataTests(SimpleTestCase):
    def test_canonical_name_trusted_with_no_fields(self) -> None:
        self.assertTrue(relevance.looks_like_parcel_data("Parcels", None))

    def test_loose_name_needs_one_corroborating_field(self) -> None:
        self.assertFalse(relevance.looks_like_parcel_data("Cadastral_Boundary", []))
        self.assertTrue(relevance.looks_like_parcel_data("Cadastral_Boundary", ["OWNER_NAME"]))

    def test_no_name_signal_needs_two_corroborating_fields(self) -> None:
        self.assertFalse(relevance.looks_like_parcel_data("", ["OWNER_NAME"]))
        self.assertTrue(relevance.looks_like_parcel_data("", ["OWNER_NAME", "PARCEL_ID"]))


class PortalItemIsPlausibleTests(SimpleTestCase):
    def test_parcel_word_in_title_passes(self) -> None:
        self.assertTrue(relevance.portal_item_is_plausible("Boone County Parcels", ""))

    def test_parcel_word_in_snippet_passes(self) -> None:
        self.assertTrue(relevance.portal_item_is_plausible("Data Layer", "Countywide tax assessment data"))

    def test_unrelated_item_is_rejected(self) -> None:
        """Regression guard: an AGOL item search for "Boone County Missouri parcels" surfaced a
        one-off watershed-analysis dataset with no parcel/assessment word anywhere in its own
        title or description - only a coincidentally-canonical sub-layer name saved it downstream."""
        self.assertFalse(relevance.portal_item_is_plausible("GBFW_data_20240926", "Data for the Greater Bonne Femme Watershed analysis 2024 - first batch 20240926"))


class UrlIsDisqualifiedTests(SimpleTestCase):
    def test_delinquent_subset_is_disqualified(self) -> None:
        self.assertTrue(relevance.url_is_disqualified("https://gis.example.gov/arcgis/rest/services/TaxDelinquentParcels/MapServer/0"))

    def test_test_layer_is_disqualified(self) -> None:
        self.assertTrue(relevance.url_is_disqualified("https://gis.example.gov/arcgis/rest/services/Parcels_Test/MapServer/0"))

    def test_ordinary_parcels_url_is_not_disqualified(self) -> None:
        self.assertFalse(relevance.url_is_disqualified("https://gis.example.gov/arcgis/rest/services/Parcels/MapServer/0"))

    def test_consent_decree_subset_is_disqualified(self) -> None:
        """Regression guard: a 3,793-row Kent County, MI groundwater-litigation parcel tracker
        (real parcels, but a sliver of the county's ~220k total) passed every other check."""
        self.assertTrue(relevance.url_is_disqualified("https://services1.arcgis.com/x/arcgis/rest/services/Parcel_Status_from_February_2020_Consent_Decree/FeatureServer/0"))


class LayerIsAcceptableTests(SimpleTestCase):
    def test_empty_fields_list_is_rejected_despite_canonical_name(self) -> None:
        """A real county's "Tax Parcels" test layer served fields: null/[] - unusable regardless of name."""
        self.assertFalse(relevance.layer_is_acceptable("Tax Parcels", []))

    def test_none_fields_is_permissive_unknown(self) -> None:
        self.assertTrue(relevance.layer_is_acceptable("Parcels", None))

    def test_non_comprehensive_subset_name_is_rejected(self) -> None:
        self.assertFalse(relevance.layer_is_acceptable("Delinquent_Tax_Parcels", ["OWNER_NAME", "PARCEL_ID"]))

    def test_non_production_marker_is_rejected(self) -> None:
        self.assertFalse(relevance.layer_is_acceptable("Parcels_Staging", ["OWNER_NAME", "PARCEL_ID"]))

    def test_ordinary_parcels_layer_is_accepted(self) -> None:
        self.assertTrue(relevance.layer_is_acceptable("Parcels", ["OWNER_NAME"]))


class CountIsSufficientTests(SimpleTestCase):
    def test_none_count_is_permissive_unknown(self) -> None:
        self.assertTrue(relevance.count_is_sufficient(None))

    def test_count_at_floor_is_sufficient(self) -> None:
        self.assertTrue(relevance.count_is_sufficient(relevance.MIN_PARCEL_FEATURE_COUNT))

    def test_count_below_floor_is_insufficient(self) -> None:
        self.assertFalse(relevance.count_is_sufficient(relevance.MIN_PARCEL_FEATURE_COUNT - 1))


class MentionsADifferentCountyTests(SimpleTestCase):
    def test_no_county_mentioned_is_unpenalized(self) -> None:
        self.assertFalse(relevance.mentions_a_different_county("Statewide Parcels", "Skagit County"))

    def test_same_county_is_not_flagged(self) -> None:
        self.assertFalse(relevance.mentions_a_different_county("Skagit County GIS Parcels", "Skagit County"))

    def test_different_county_is_flagged(self) -> None:
        self.assertTrue(relevance.mentions_a_different_county("Thurston County Parcels Export", "Skagit County"))


class MentionsADifferentStateTests(SimpleTestCase):
    def test_no_target_state_disables_check(self) -> None:
        self.assertFalse(relevance.mentions_a_different_state("Douglas County, OR", ""))

    def test_same_state_full_name_is_not_flagged(self) -> None:
        self.assertFalse(relevance.mentions_a_different_state("Douglas County, Oregon GIS", "Oregon"))

    def test_different_state_full_name_is_flagged(self) -> None:
        self.assertTrue(relevance.mentions_a_different_state("Florida Statewide Parcels", "Washington"))

    def test_state_name_immediately_followed_by_county_is_not_flagged(self) -> None:
        """"Washington County Parcels, Minnesota" must not read as a reference to Washington state."""
        self.assertFalse(relevance.mentions_a_different_state("Washington County Parcels", "Minnesota"))

    def test_different_state_abbreviation_after_comma_is_flagged(self) -> None:
        """Regression guard: the real incident text never spells out "Oregon", only "Douglas County, OR"."""
        self.assertTrue(relevance.mentions_a_different_state("Geographic Information Systems (GIS) | Douglas County, OR", "Nebraska"))

    def test_same_state_abbreviation_after_comma_is_not_flagged(self) -> None:
        self.assertFalse(relevance.mentions_a_different_state("Douglas County, NE GIS parcel data", "Nebraska"))

    def test_lowercase_or_is_not_mistaken_for_oregon(self) -> None:
        """Case-sensitivity guard: the literal word "or" must never be read as the state abbreviation."""
        self.assertFalse(relevance.mentions_a_different_state("ArcGIS REST or Socrata open data, Nebraska", "Nebraska"))

    def test_bare_abbreviation_without_comma_is_not_flagged(self) -> None:
        self.assertFalse(relevance.mentions_a_different_state("GIS OR Socrata", "Nebraska"))


class ArcgisExtentAndWkidTests(SimpleTestCase):
    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(relevance.arcgis_extent_and_wkid(None))

    def test_missing_coordinates_returns_none(self) -> None:
        self.assertIsNone(relevance.arcgis_extent_and_wkid({"spatialReference": {"wkid": 4326}}))

    def test_missing_spatial_reference_returns_none(self) -> None:
        self.assertIsNone(relevance.arcgis_extent_and_wkid({"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4}))

    def test_prefers_latest_wkid_over_wkid(self) -> None:
        extent = {"xmin": 1.0, "ymin": 2.0, "xmax": 3.0, "ymax": 4.0, "spatialReference": {"wkid": 102100, "latestWkid": 3857}}
        result = relevance.arcgis_extent_and_wkid(extent)
        self.assertEqual(result, ((1.0, 2.0, 3.0, 4.0), 3857))

    def test_valid_extent_is_extracted(self) -> None:
        extent = {"xmin": -92.5, "ymin": 38.6, "xmax": -92.1, "ymax": 39.2, "spatialReference": {"wkid": 4326}}
        result = relevance.arcgis_extent_and_wkid(extent)
        self.assertEqual(result, ((-92.5, 38.6, -92.1, 39.2), 4326))


class ExtentOverlapsCountyTests(SimpleTestCase):
    def test_unknown_layer_extent_is_permissive(self) -> None:
        self.assertTrue(relevance.extent_overlaps_county(None, (0, 0, 1, 1)))

    def test_unknown_county_extent_is_permissive(self) -> None:
        self.assertTrue(relevance.extent_overlaps_county((0, 0, 1, 1), None))

    def test_overlapping_boxes_are_confirmed(self) -> None:
        self.assertTrue(relevance.extent_overlaps_county((0, 0, 2, 2), (1, 1, 3, 3)))

    def test_disjoint_boxes_are_rejected(self) -> None:
        """Regression guard: Nicholas County, WV and Boone County, MO don't overlap at all."""
        west_virginia_extent = (-81.5, 38.2, -80.9, 38.6)
        missouri_extent = (-92.57, 38.64, -92.10, 39.25)
        self.assertFalse(relevance.extent_overlaps_county(west_virginia_extent, missouri_extent))
