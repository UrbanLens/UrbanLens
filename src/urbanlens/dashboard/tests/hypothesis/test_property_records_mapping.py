"""Tests for the property-records Tier 1 field-mapping/normalization layer.

Covers:
- field_mapping.map_fields: heuristic raw-attribute-name resolution, the
  per-jurisdiction field_map override, and the acres->sqft lot-size fallback.
- normalize.build_property_record: defensive type coercion (ArcGIS epoch-ms
  dates, numeric strings, "OWNER1 & OWNER2" splitting) that never raises on
  malformed county data.
- schema.PropertyRecord.to_dict(): JSON-serializable output shape.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.arcgis_socrata import GEOMETRY_KEY
from urbanlens.dashboard.services.apis.property_records.field_mapping import _HEURISTIC_CANDIDATES, _SUPPLEMENTARY_CANDIDATES, SQFT_PER_ACRE, map_fields
from urbanlens.dashboard.services.apis.property_records.normalize import TIER1_CONFIDENCE, _split_owner_names, _to_date, _to_float, build_property_record
from urbanlens.dashboard.services.apis.property_records.schema import AssessedValue, BuildingCharacteristics, PropertyRecord, RecordSource


class MapFieldsHeuristicTests(SimpleTestCase):
    def test_resolves_common_apn_spelling(self) -> None:
        mapped = map_fields({"PARCELID": "123-456", "OTHER": "x"})
        self.assertEqual(mapped["apn"], "123-456")

    def test_resolves_case_and_punctuation_insensitively(self) -> None:
        mapped = map_fields({"Owner Name": "Jane Smith"})
        self.assertEqual(mapped["owner_name"], "Jane Smith")

    def test_unmatched_fields_are_absent_not_none(self) -> None:
        mapped = map_fields({"SOME_UNRELATED_FIELD": "value"})
        self.assertNotIn("apn", mapped)

    def test_field_map_override_wins_over_heuristics(self) -> None:
        # PARCELID would normally match "apn" - the explicit override should
        # instead point "apn" at a nonstandard raw key.
        raw = {"PARCELID": "wrong", "WEIRD_LOCAL_ID": "right"}
        mapped = map_fields(raw, field_map={"apn": "WEIRD_LOCAL_ID"})
        self.assertEqual(mapped["apn"], "right")

    def test_field_map_override_pointing_at_a_missing_key_is_dropped(self) -> None:
        mapped = map_fields({"PARCELID": "123"}, field_map={"apn": "DOES_NOT_EXIST"})
        self.assertNotIn("apn", mapped)

    def test_lot_size_prefers_direct_sqft_field(self) -> None:
        mapped = map_fields({"LOTSIZE": 5000, "ACREAGE": 1})
        self.assertEqual(mapped["lot_size_sqft"], 5000)

    def test_lot_size_converts_acres_when_no_sqft_field(self) -> None:
        mapped = map_fields({"ACREAGE": 2})
        self.assertEqual(mapped["lot_size_sqft"], 2 * SQFT_PER_ACRE)

    def test_lot_size_absent_when_neither_field_present(self) -> None:
        mapped = map_fields({})
        self.assertNotIn("lot_size_sqft", mapped)

    def test_non_numeric_acreage_does_not_raise(self) -> None:
        mapped = map_fields({"ACREAGE": "not-a-number"})
        self.assertNotIn("lot_size_sqft", mapped)

    @given(st.floats(min_value=0.01, max_value=10_000, allow_nan=False, allow_infinity=False))
    def test_acre_to_sqft_conversion_is_linear(self, acres: float) -> None:
        mapped = map_fields({"ACREAGE": acres})
        self.assertAlmostEqual(mapped["lot_size_sqft"], acres * SQFT_PER_ACRE)


class SupplementaryFieldMappingTests(SimpleTestCase):
    """The new retrieval-only fields resolve, and never leak into discovery's candidate pool."""

    def test_core_candidates_exclude_every_supplementary_key(self) -> None:
        """Regression guard: relevance.PARCEL_FIELD_CANDIDATES is built from _HEURISTIC_CANDIDATES
        only. A live discovery false positive (Pima County, AZ's single-family-only subset) had
        STORIES/ROOF/GARAGE/ZONING-shaped fields - folding those into the same pool discovery uses
        to judge "is this comprehensive parcel data" would make that exact false positive pass
        again, so the two candidate pools must stay disjoint."""
        self.assertEqual(set(_HEURISTIC_CANDIDATES) & set(_SUPPLEMENTARY_CANDIDATES), set())

    def test_zoning_code_resolves(self) -> None:
        self.assertEqual(map_fields({"ZONING": "R-1"})["zoning_code"], "R-1")

    def test_tax_district_resolves(self) -> None:
        self.assertEqual(map_fields({"TAX_DIST": "12"})["tax_district"], "12")

    def test_school_district_resolves(self) -> None:
        self.assertEqual(map_fields({"SCHOOL_DIST": "Unified 5"})["school_district"], "Unified 5")

    def test_exemption_type_resolves(self) -> None:
        self.assertEqual(map_fields({"EXEMPT_CODE": "HOMESTEAD"})["exemption_type"], "HOMESTEAD")

    def test_deferred_value_resolves(self) -> None:
        self.assertEqual(map_fields({"DEFERRED_VALUE": 15000})["deferred_value"], 15000)

    def test_subdivision_name_resolves(self) -> None:
        self.assertEqual(map_fields({"SUBDIV_NAME": "Oak Hills"})["subdivision_name"], "Oak Hills")

    def test_neighborhood_resolves(self) -> None:
        self.assertEqual(map_fields({"NBH_NAME": "Downtown"})["neighborhood"], "Downtown")

    def test_prior_parcel_id_resolves(self) -> None:
        self.assertEqual(map_fields({"OLDPIN": "OLD-123"})["prior_parcel_id"], "OLD-123")

    def test_co_owner_name_resolves(self) -> None:
        self.assertEqual(map_fields({"COOWNER": "John Smith"})["co_owner_name"], "John Smith")

    def test_owner_mailing_city_state_zip_resolve(self) -> None:
        mapped = map_fields({"OWNER_CITY": "Springfield", "OWNER_STAT": "IL", "OWNER_ZIP": "62701"})
        self.assertEqual(mapped["owner_mailing_city"], "Springfield")
        self.assertEqual(mapped["owner_mailing_state"], "IL")
        self.assertEqual(mapped["owner_mailing_zip"], "62701")

    def test_building_characteristic_fields_resolve(self) -> None:
        mapped = map_fields(
            {
                "STORIES": 2,
                "ROOF": "Asphalt Shingle",
                "WALLS": "Brick",
                "GARAGE": "Attached 2-car",
                "HEAT": "Forced Air",
                "QUALITY": "Average",
                "CONDITION": "Good",
                "NUMBLDGS": 1,
                "OBXF_VALUE": 5000,
            },
        )
        self.assertEqual(mapped["building_stories"], 2)
        self.assertEqual(mapped["roof_material"], "Asphalt Shingle")
        self.assertEqual(mapped["wall_material"], "Brick")
        self.assertEqual(mapped["garage"], "Attached 2-car")
        self.assertEqual(mapped["heating_type"], "Forced Air")
        self.assertEqual(mapped["building_quality"], "Average")
        self.assertEqual(mapped["building_condition"], "Good")
        self.assertEqual(mapped["building_count"], 1)
        self.assertEqual(mapped["outbuilding_value"], 5000)


class RealCountyFieldSpellingTests(SimpleTestCase):
    """Regression guard: Chester County, PA - a jurisdiction the discovery pipeline had
    specifically confirmed as genuinely comprehensive parcel data - used UPI/OWN1/OWN2/
    LOC_ADDRESS/TOT_ASSESS/TAXYR, none of which matched any candidate before this fix, so every
    core field (owner, address, APN, assessed value) silently came back empty for it. Found live
    while verifying this module's own geometry-capture code against real data."""

    def test_upi_resolves_to_apn(self) -> None:
        self.assertEqual(map_fields({"UPI": "12-3-45"})["apn"], "12-3-45")

    def test_own1_resolves_to_owner_name(self) -> None:
        self.assertEqual(map_fields({"OWN1": "Jane Smith"})["owner_name"], "Jane Smith")

    def test_own2_resolves_to_co_owner_name(self) -> None:
        self.assertEqual(map_fields({"OWN2": "John Smith"})["co_owner_name"], "John Smith")

    def test_loc_address_resolves_to_situs_address(self) -> None:
        self.assertEqual(map_fields({"LOC_ADDRESS": "123 Main St"})["situs_address"], "123 Main St")

    def test_tot_assess_resolves_to_assessed_total(self) -> None:
        self.assertEqual(map_fields({"TOT_ASSESS": 250000})["assessed_total"], 250000)

    def test_taxyr_resolves_to_assessed_year(self) -> None:
        self.assertEqual(map_fields({"TAXYR": 2024})["assessed_year"], 2024)


class SplitOwnerNamesTests(SimpleTestCase):
    def test_single_owner_is_a_one_item_tuple(self) -> None:
        self.assertEqual(_split_owner_names("Jane Smith"), ("Jane Smith",))

    def test_ampersand_splits_two_owners(self) -> None:
        self.assertEqual(_split_owner_names("John Smith & Jane Smith"), ("John Smith", "Jane Smith"))

    def test_and_splits_case_insensitively(self) -> None:
        self.assertEqual(_split_owner_names("John Smith and Jane Smith".upper()), ("JOHN SMITH", "JANE SMITH"))

    def test_blank_input_is_empty_tuple(self) -> None:
        self.assertEqual(_split_owner_names(""), ())

    def test_non_string_input_does_not_raise(self) -> None:
        self.assertEqual(_split_owner_names(None), ())


class ToDateTests(SimpleTestCase):
    def test_arcgis_epoch_milliseconds_is_parsed(self) -> None:
        # 2020-01-01T00:00:00Z in epoch ms.
        self.assertEqual(_to_date(1577836800000).isoformat(), "2020-01-01")

    def test_iso_date_string_is_parsed(self) -> None:
        self.assertEqual(_to_date("2020-06-15").isoformat(), "2020-06-15")

    def test_us_slash_format_is_parsed(self) -> None:
        self.assertEqual(_to_date("06/15/2020").isoformat(), "2020-06-15")

    def test_garbage_does_not_raise(self) -> None:
        self.assertIsNone(_to_date("not a date"))

    def test_none_is_none(self) -> None:
        self.assertIsNone(_to_date(None))


class ToFloatTests(SimpleTestCase):
    @given(st.one_of(st.text(), st.none(), st.booleans(), st.dictionaries(st.text(), st.text())))
    def test_never_raises_regardless_of_input_type(self, value) -> None:
        _to_float(value)  # Only asserting this doesn't raise.

    def test_numeric_string_parses(self) -> None:
        self.assertEqual(_to_float("1234.5"), 1234.5)


class BuildPropertyRecordTests(SimpleTestCase):
    def _jurisdiction(self, **overrides):
        from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

        defaults = {"fips": "36001", "county_name": "Albany County", "state": "NY", "field_map": {}}
        defaults.update(overrides)
        return PropertyJurisdiction(**defaults)

    def test_builds_record_from_typical_arcgis_attributes(self) -> None:
        raw = {
            "PARCELID": "12.34-5-6",
            "OWNERNME1": "Jane Smith",
            "SITUS_ADDR": "123 Main St",
            "TOTALVAL": "250000",
            "LANDVAL": "50000",
            "IMPVAL": "200000",
            "YEARBUILT": "1985",
        }
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST", source_url="https://example.gov/MapServer/1")

        self.assertEqual(record.apn, "12.34-5-6")
        self.assertEqual(record.owner_name, ("Jane Smith",))
        self.assertEqual(record.situs_address, "123 Main St")
        self.assertEqual(record.year_built, 1985)
        assert record.assessed_value is not None
        self.assertEqual(record.assessed_value.total, 250000.0)
        self.assertEqual(record.county, "Albany County")
        self.assertEqual(record.state, "NY")
        self.assertEqual(record.fips, "36001")
        self.assertEqual(record.source.tier, 1)
        self.assertEqual(record.confidence, TIER1_CONFIDENCE)

    def test_sparse_attributes_still_build_a_record(self) -> None:
        record = build_property_record({}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.situs_address, "")
        self.assertEqual(record.owner_name, ())
        self.assertIsNone(record.assessed_value)

    def test_malformed_values_are_dropped_not_raised(self) -> None:
        raw = {"YEARBUILT": "not-a-year", "TOTALVAL": "N/A"}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertIsNone(record.year_built)
        self.assertIsNone(record.assessed_value)

    def test_field_map_override_is_applied(self) -> None:
        record = build_property_record({"CUSTOM_OWNER_FIELD": "Bob Jones"}, jurisdiction=self._jurisdiction(field_map={"owner_name": "CUSTOM_OWNER_FIELD"}), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.owner_name, ("Bob Jones",))

    def test_co_owner_is_appended_to_owner_name(self) -> None:
        record = build_property_record({"OWNER": "Jane Smith", "COOWNER": "John Smith"}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.owner_name, ("Jane Smith", "John Smith"))

    def test_co_owner_duplicate_of_primary_owner_is_not_appended_twice(self) -> None:
        record = build_property_record({"OWNER": "Jane Smith", "COOWNER": "jane smith"}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.owner_name, ("Jane Smith",))

    def test_mailing_address_prefers_combined_field_over_composed_parts(self) -> None:
        raw = {"MAILADDR": "123 Elm St, Anytown, CA 90210", "OWNER_CITY": "Wrongtown"}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.owner_mailing_address, "123 Elm St, Anytown, CA 90210")

    def test_mailing_address_is_composed_from_separate_city_state_zip(self) -> None:
        raw = {"OWNER_CITY": "Springfield", "OWNER_STAT": "IL", "OWNER_ZIP": "62701"}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.owner_mailing_address, "Springfield, IL 62701")

    def test_no_mailing_fields_at_all_is_none(self) -> None:
        record = build_property_record({}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertIsNone(record.owner_mailing_address)

    def test_building_characteristics_built_when_any_field_present(self) -> None:
        raw = {"STORIES": 2, "ROOF": "Metal", "GARAGE": "Detached", "NUMBLDGS": 2, "OBXF_VALUE": 3000}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        assert record.building_characteristics is not None
        self.assertEqual(record.building_characteristics.stories, 2.0)
        self.assertEqual(record.building_characteristics.roof_material, "Metal")
        self.assertEqual(record.building_characteristics.garage, "Detached")
        self.assertEqual(record.building_characteristics.building_count, 2)
        self.assertEqual(record.building_characteristics.outbuilding_value, 3000.0)

    def test_building_characteristics_absent_when_no_field_present(self) -> None:
        record = build_property_record({}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertIsNone(record.building_characteristics)

    def test_prior_parcel_id_becomes_a_one_item_tuple(self) -> None:
        record = build_property_record({"OLDPIN": "OLD-1"}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.prior_parcel_ids, ("OLD-1",))

    def test_no_prior_parcel_id_is_empty_tuple(self) -> None:
        record = build_property_record({}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.prior_parcel_ids, ())

    def test_zoning_district_exemption_fields_are_mapped(self) -> None:
        raw = {"ZONING": "R-1", "TAX_DIST": "12", "SCHOOL_DIST": "Unified 5", "EXEMPT_CODE": "HOMESTEAD", "DEFERRED_VALUE": 5000, "SUBDIV_NAME": "Oak Hills", "NBH_NAME": "Downtown"}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.zoning_code, "R-1")
        self.assertEqual(record.tax_district, "12")
        self.assertEqual(record.school_district, "Unified 5")
        self.assertEqual(record.exemption_type, "HOMESTEAD")
        self.assertEqual(record.deferred_value, 5000.0)
        self.assertEqual(record.subdivision_name, "Oak Hills")
        self.assertEqual(record.neighborhood, "Downtown")

    def test_geometry_sentinel_key_becomes_parcel_geometry_and_is_excluded_from_field_mapping(self) -> None:
        geometry = {"format": "esri_rings", "spatial_reference": "EPSG:4326", "rings": [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1], [-82.0, 39.0]]]}
        raw = {"OWNER": "Jane Smith", GEOMETRY_KEY: geometry}
        record = build_property_record(raw, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertEqual(record.parcel_geometry, geometry)
        self.assertEqual(record.owner_name, ("Jane Smith",))

    def test_no_geometry_key_is_none(self) -> None:
        record = build_property_record({"OWNER": "Jane Smith"}, jurisdiction=self._jurisdiction(), tier=1, confidence=TIER1_CONFIDENCE, provider="ArcGIS REST")
        self.assertIsNone(record.parcel_geometry)


class PropertyRecordToDictTests(SimpleTestCase):
    def test_to_dict_is_json_serializable_shape(self) -> None:
        import json

        record = PropertyRecord(
            situs_address="123 Main St",
            county="Albany County",
            state="NY",
            fips="36001",
            source=RecordSource(tier=1, provider="ArcGIS REST", url="https://example.gov"),
            confidence=0.7,
            apn="1-2-3",
            owner_name=("Jane Smith",),
            assessed_value=AssessedValue(year=2024, land=1.0, improvement=2.0, total=3.0),
        )
        serialized = json.dumps(record.to_dict())
        payload = json.loads(serialized)
        self.assertEqual(payload["apn"], "1-2-3")
        self.assertEqual(payload["owner_name"], ["Jane Smith"])
        self.assertEqual(payload["assessed_value"]["total"], 3.0)
        self.assertEqual(payload["source"]["tier"], 1)

    def test_empty_record_serializes_with_none_optionals(self) -> None:
        record = PropertyRecord(
            situs_address="",
            county="",
            state="",
            fips="",
            source=RecordSource(tier=1, provider="ArcGIS REST"),
            confidence=0.0,
        )
        payload = record.to_dict()
        self.assertIsNone(payload["assessed_value"])
        self.assertEqual(payload["owner_name"], [])
        self.assertEqual(payload["sales_history"], [])
        self.assertIsNone(payload["building_characteristics"])
        self.assertIsNone(payload["parcel_geometry"])
        self.assertEqual(payload["prior_parcel_ids"], [])

    def test_new_fields_round_trip_through_to_dict(self) -> None:
        import json

        geometry = {"format": "esri_rings", "spatial_reference": "EPSG:4326", "rings": [[[-82.0, 39.0], [-82.0, 39.1], [-81.9, 39.1], [-82.0, 39.0]]]}
        record = PropertyRecord(
            situs_address="123 Main St",
            county="Albany County",
            state="NY",
            fips="36001",
            source=RecordSource(tier=1, provider="ArcGIS REST"),
            confidence=0.7,
            zoning_code="R-1",
            tax_district="12",
            school_district="Unified 5",
            exemption_type="HOMESTEAD",
            deferred_value=5000.0,
            subdivision_name="Oak Hills",
            neighborhood="Downtown",
            building_characteristics=BuildingCharacteristics(stories=2.0, roof_material="Metal", building_count=1),
            prior_parcel_ids=("OLD-1",),
            parcel_geometry=geometry,
        )
        payload = json.loads(json.dumps(record.to_dict()))
        self.assertEqual(payload["zoning_code"], "R-1")
        self.assertEqual(payload["tax_district"], "12")
        self.assertEqual(payload["school_district"], "Unified 5")
        self.assertEqual(payload["exemption_type"], "HOMESTEAD")
        self.assertEqual(payload["deferred_value"], 5000.0)
        self.assertEqual(payload["subdivision_name"], "Oak Hills")
        self.assertEqual(payload["neighborhood"], "Downtown")
        self.assertEqual(payload["building_characteristics"]["stories"], 2.0)
        self.assertEqual(payload["building_characteristics"]["roof_material"], "Metal")
        self.assertEqual(payload["prior_parcel_ids"], ["OLD-1"])
        self.assertEqual(payload["parcel_geometry"], geometry)
