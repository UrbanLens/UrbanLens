"""Tests for the REData building-attributes plugin.

Retrieval calls REData's parcel/buildings endpoints (see the module docstring
in plugins.builtin.redata_building_attributes) - RedataGateway itself is
mocked, so no real network access occurs. Covers nearest-building selection
among multiple returned buildings, fetch()'s graceful degradation, and
render_context/plugin-contribution shapes.
"""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.redata_building_attributes import (
    RedataBuildingAttributesEnrichmentSource,
    RedataBuildingAttributesPanelSource,
    RedataBuildingAttributesPlugin,
    _fetch_building_payload,
    _nearest_building,
    _render_building_attributes,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

_NEAR_BUILDING = {"source": "cris", "name": "Old Mill", "building_number": "72", "year_built": 1937, "latitude": 42.6501, "longitude": -73.7501}
_FAR_BUILDING = {"source": "county_gis", "name": "Warehouse B", "building_number": "", "year_built": None, "latitude": 43.0, "longitude": -74.0}


class NearestBuildingTests(SimpleTestCase):
    def test_picks_the_closest_building(self) -> None:
        self.assertEqual(_nearest_building([_FAR_BUILDING, _NEAR_BUILDING], 42.65, -73.75), _NEAR_BUILDING)

    def test_single_building_is_returned(self) -> None:
        self.assertEqual(_nearest_building([_FAR_BUILDING], 42.65, -73.75), _FAR_BUILDING)

    def test_empty_list_returns_none(self) -> None:
        self.assertIsNone(_nearest_building([], 42.65, -73.75))

    def test_building_missing_coordinates_is_never_picked_over_one_with_coordinates(self) -> None:
        no_coords = {"name": "Mystery Building"}
        self.assertEqual(_nearest_building([no_coords, _NEAR_BUILDING], 42.65, -73.75), _NEAR_BUILDING)


class FetchBuildingPayloadTests(TestCase):
    def test_returns_the_nearest_building(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=[_FAR_BUILDING, _NEAR_BUILDING]),
        ):
            payload = _fetch_building_payload(42.65, -73.75)
        self.assertEqual(payload, _NEAR_BUILDING)

    def test_no_parcel_returns_empty_dict(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value=None),
        ):
            self.assertEqual(_fetch_building_payload(42.65, -73.75), {})

    def test_no_buildings_returns_empty_dict(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=[]),
        ):
            self.assertEqual(_fetch_building_payload(42.65, -73.75), {})

    def test_unavailable_gracefully_returns_empty_dict(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", side_effect=PropertyRecordsUnavailableError("source_error", "boom")),
        ):
            self.assertEqual(_fetch_building_payload(42.65, -73.75), {})

    def test_unconfigured_gateway_gracefully_returns_empty_dict(self) -> None:
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured.

        The unconfigured state is simulated rather than left to the ambient
        environment: an install that *does* configure REData (any dev machine
        with UL_REDATA_API_URL set) would otherwise reach the real API here
        instead of exercising this branch. ``__post_init__`` is what raises
        that ValueError, and it's the only patchable seam - RedataGateway is a
        slotted dataclass, so ``base_url`` itself is read-only on the class.
        """
        with patch.object(RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")):
            self.assertEqual(_fetch_building_payload(42.65, -73.75), {})


class RenderBuildingAttributesTests(SimpleTestCase):
    def test_empty_data_yields_none(self) -> None:
        self.assertIsNone(_render_building_attributes({}))

    def test_no_relevant_fields_yields_none(self) -> None:
        self.assertIsNone(_render_building_attributes({"source": "county_gis", "address": "123 Main St"}))

    def test_full_payload_renders_expected_fields(self) -> None:
        ctx = _render_building_attributes(_NEAR_BUILDING)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Old Mill")
        labels = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels["Building Number"], "72")
        self.assertEqual(labels["Year Built"], 1937)
        self.assertEqual(ctx["chips"], ["NY SHPO (CRIS)"])

    def test_building_number_alone_still_renders(self) -> None:
        ctx = _render_building_attributes({"building_number": "5"})
        assert ctx is not None
        self.assertIsNone(ctx["heading_name"])
        self.assertEqual(ctx["meta"], [{"label": "Building Number", "value": "5"}])

    def test_unknown_source_yields_no_chip(self) -> None:
        ctx = _render_building_attributes({"name": "Mystery", "source": "unknown_provider"})
        assert ctx is not None
        self.assertEqual(ctx["chips"], [])


def _make_profile():
    return baker.make("dashboard.Profile")


class PanelFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        self.pin = baker.make(Pin, profile=_make_profile(), location=self.location)

    def test_fetch_caches_the_nearest_building(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=[_NEAR_BUILDING]),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as mock_set,
        ):
            RedataBuildingAttributesPanelSource().fetch(self.pin)
        mock_set.assert_called_once_with(self.location, "redata_building_attributes", _NEAR_BUILDING, query_key="42.65000,-73.75000")

    def test_render_context_delegates_to_shared_renderer(self) -> None:
        ctx = RedataBuildingAttributesPanelSource().render_context(self.pin, _NEAR_BUILDING)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Old Mill")

    def test_render_context_empty_data_yields_none(self) -> None:
        self.assertIsNone(RedataBuildingAttributesPanelSource().render_context(self.pin, {}))

    def test_render_context_is_suppressed_for_a_parcel_scope_pin(self) -> None:
        """The building nearest a campus marker is one arbitrary structure of dozens."""
        from urbanlens.dashboard.models.pin.model import PinType

        self.pin.pin_type = PinType.PARCEL
        self.pin.pin_type_is_user_provided = True
        self.assertIsNone(RedataBuildingAttributesPanelSource().render_context(self.pin, _NEAR_BUILDING))

    def test_fetch_reuses_a_cached_parcel_building_list(self) -> None:
        """The parcel_buildings plugin already made this exact REData call."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": [_FAR_BUILDING, _NEAR_BUILDING]})
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid") as mock_lookup,
        ):
            RedataBuildingAttributesPanelSource().fetch(self.pin)
        mock_lookup.assert_not_called()
        cached = LocationCache.get_fresh(self.location, "redata_building_attributes")
        assert cached is not None
        self.assertEqual(cached.data, _NEAR_BUILDING)


class EnrichmentSourceTests(TestCase):
    def test_fetch_returns_the_nearest_building_and_query_key(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=[_NEAR_BUILDING]),
        ):
            payload, query_key = RedataBuildingAttributesEnrichmentSource().fetch(location)
        self.assertEqual(payload, _NEAR_BUILDING)
        self.assertEqual(query_key, "42.65000,-73.75000")

    def test_fetch_returns_empty_dict_when_unconfigured(self) -> None:
        location = baker.make(Location, latitude="42.650000", longitude="-73.750000", google_place=None)
        with patch.object(RedataGateway, "__post_init__", side_effect=ValueError("UL_REDATA_API_URL must be configured.")):
            payload, _query_key = RedataBuildingAttributesEnrichmentSource().fetch(location)
        self.assertEqual(payload, {})


class PluginContributionsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plugin = RedataBuildingAttributesPlugin()

    def test_contributes_one_panel_source(self) -> None:
        sources = self.plugin.get_panel_sources()
        self.assertEqual([type(source) for source in sources], [RedataBuildingAttributesPanelSource])

    def test_contributes_one_enrichment_source(self) -> None:
        sources = self.plugin.get_enrichment_sources()
        self.assertEqual([type(source) for source in sources], [RedataBuildingAttributesEnrichmentSource])

    def test_contributes_a_name_provider_reading_the_building_name(self) -> None:
        providers = self.plugin.get_name_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].source, "redata_building")
        self.assertEqual(providers[0].cache_source, "redata_building_attributes")
        self.assertEqual(providers[0].keys, ("name",))
