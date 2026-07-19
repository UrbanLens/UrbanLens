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
from urbanlens.dashboard.services.apis.property_records.field_mapping import SQFT_PER_ACRE, map_fields
from urbanlens.dashboard.services.apis.property_records.normalize import TIER1_CONFIDENCE, _split_owner_names, _to_date, _to_float, build_property_record
from urbanlens.dashboard.services.apis.property_records.schema import AssessedValue, PropertyRecord, RecordSource


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
