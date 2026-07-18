"""Tests for jurisdiction resolution and the tiered orchestrator dispatch.

Covers:
- jurisdiction.resolve_jurisdiction: USA-only gating, TIGERweb-backed FIPS
  resolution, and get-or-create idempotency against the registry.
- orchestrator.get_property_record: dispatches to Tier 1 for
  ARCGIS_REST/SOCRATA rows and raises PropertyRecordsUnavailableError with the
  correct reason for every other adapter_type, without ever touching the
  network for tiers that aren't implemented.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType
from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
from urbanlens.dashboard.services.apis.property_records import jurisdiction as jurisdiction_module, orchestrator
from urbanlens.dashboard.services.apis.property_records.orchestrator import (
    REASON_MANUAL_ONLY,
    REASON_NO_DATA_FOUND,
    REASON_OUTSIDE_COVERAGE,
    REASON_TIER2_NOT_IMPLEMENTED,
    REASON_TIER3_NOT_IMPLEMENTED,
    REASON_UNRESEARCHED,
    PropertyRecordsUnavailableError,
    get_property_record,
)


class ResolveJurisdictionTests(TestCase):
    def test_non_usa_coordinates_short_circuit_without_calling_tigerweb(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway") as gateway_cls:
            result = jurisdiction_module.resolve_jurisdiction(48.8566, 2.3522)  # Paris
        self.assertIsNone(result)
        gateway_cls.assert_not_called()

    def test_no_county_geography_returns_none(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway") as gateway_cls:
            gateway_cls.return_value.get_geography.return_value = {"state": {"name": "New York", "geoid": "36"}, "county": None}
            result = jurisdiction_module.resolve_jurisdiction(42.65, -73.75)
        self.assertIsNone(result)

    def test_creates_a_registry_row_from_tigerweb_geography(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway") as gateway_cls:
            gateway_cls.return_value.get_geography.return_value = {
                "state": {"name": "New York", "geoid": "36"},
                "county": {"name": "Albany County", "geoid": "36001"},
            }
            result = jurisdiction_module.resolve_jurisdiction(42.65, -73.75)
        assert result is not None
        self.assertEqual(result.fips, "36001")
        self.assertEqual(result.county_name, "Albany County")
        self.assertEqual(result.state, "NY")
        self.assertEqual(result.adapter_type, AdapterType.UNKNOWN)

    def test_second_call_reuses_the_same_row(self) -> None:
        geography = {"state": {"name": "New York", "geoid": "36"}, "county": {"name": "Albany County", "geoid": "36001"}}
        with mock.patch("urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway") as gateway_cls:
            gateway_cls.return_value.get_geography.return_value = geography
            first = jurisdiction_module.resolve_jurisdiction(42.65, -73.75)
            second = jurisdiction_module.resolve_jurisdiction(42.65, -73.75)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PropertyJurisdiction.objects.filter(fips="36001").count(), 1)

    def test_an_existing_rows_adapter_configuration_is_never_overwritten(self) -> None:
        PropertyJurisdiction.objects.create(fips="36001", county_name="Albany County", state="NY", adapter_type=AdapterType.ARCGIS_REST, gis_rest_url="https://example.gov/MapServer/1")
        geography = {"state": {"name": "New York", "geoid": "36"}, "county": {"name": "Albany County", "geoid": "36001"}}
        with mock.patch("urbanlens.dashboard.services.apis.locations.census_tigerweb.CensusTigerwebGateway") as gateway_cls:
            gateway_cls.return_value.get_geography.return_value = geography
            result = jurisdiction_module.resolve_jurisdiction(42.65, -73.75)
        self.assertEqual(result.adapter_type, AdapterType.ARCGIS_REST)
        self.assertEqual(result.gis_rest_url, "https://example.gov/MapServer/1")


class GetPropertyRecordDispatchTests(TestCase):
    def _make_jurisdiction(self, **overrides) -> PropertyJurisdiction:
        defaults = {"fips": "36001", "county_name": "Albany County", "state": "NY"}
        defaults.update(overrides)
        return PropertyJurisdiction.objects.create(**defaults)

    def test_unresolvable_coordinate_raises_outside_coverage(self) -> None:
        with mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=None), self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            get_property_record(0.0, 0.0)
        self.assertEqual(ctx.exception.reason, REASON_OUTSIDE_COVERAGE)

    def test_unknown_adapter_type_raises_unresearched(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.UNKNOWN)
        with mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row), self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            get_property_record(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_UNRESEARCHED)

    def test_known_vendor_raises_tier2_not_implemented_without_any_network_call(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.KNOWN_VENDOR, vendor="tyler")
        with mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row), mock.patch("urbanlens.dashboard.services.apis.property_records.arcgis_socrata.ArcGisSocrataGateway") as gw:
            with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                get_property_record(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_TIER2_NOT_IMPLEMENTED)
        gw.assert_not_called()

    def test_custom_scraper_raises_tier3_not_implemented(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.CUSTOM_SCRAPER)
        with mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row), self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            get_property_record(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_TIER3_NOT_IMPLEMENTED)

    def test_manual_only_raises_with_configured_instructions(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.MANUAL_ONLY, manual_instructions="Call the assessor's office at 555-1234.")
        with mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row), self.assertRaises(PropertyRecordsUnavailableError) as ctx:
            get_property_record(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_MANUAL_ONLY)
        self.assertIn("555-1234", str(ctx.exception))

    def test_tier1_success_returns_a_normalized_record(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.ARCGIS_REST, gis_rest_url="https://example.gov/MapServer/1")
        raw_attrs = [{"PARCELID": "1-2-3", "OWNERNME1": "Jane Smith"}]
        with (
            mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row),
            mock.patch("urbanlens.dashboard.services.apis.property_records.orchestrator.ArcGisSocrataGateway") as gw_cls,
        ):
            gw_cls.return_value.query_by_point.return_value = raw_attrs
            record = get_property_record(42.65, -73.75)
        self.assertEqual(record.apn, "1-2-3")
        self.assertEqual(record.owner_name, ("Jane Smith",))
        self.assertEqual(record.source.tier, 1)

    def test_tier1_no_results_raises_no_data_found(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.ARCGIS_REST, gis_rest_url="https://example.gov/MapServer/1")
        with (
            mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row),
            mock.patch("urbanlens.dashboard.services.apis.property_records.orchestrator.ArcGisSocrataGateway") as gw_cls,
        ):
            gw_cls.return_value.query_by_point.return_value = []
            with self.assertRaises(PropertyRecordsUnavailableError) as ctx:
                get_property_record(42.65, -73.75)
        self.assertEqual(ctx.exception.reason, REASON_NO_DATA_FOUND)

    def test_socrata_adapter_uses_socrata_provider_label(self) -> None:
        row = self._make_jurisdiction(adapter_type=AdapterType.SOCRATA, gis_rest_url="https://data.example.gov/resource/abcd-1234.json", gis_geo_field="location")
        with (
            mock.patch.object(orchestrator, "resolve_jurisdiction", return_value=row),
            mock.patch("urbanlens.dashboard.services.apis.property_records.orchestrator.ArcGisSocrataGateway") as gw_cls,
        ):
            gw_cls.return_value.query_by_point.return_value = [{"apn": "1"}]
            record = get_property_record(42.65, -73.75)
        self.assertEqual(record.source.provider, "Socrata")
