"""Tests for the parcel-buildings plugin.

Covers the REData-then-Overpass provider order, the panel's gate, the row
builder that pairs each building with the child marker covering it, and the
"Buildings on this Property" panel endpoint on both the pin and wiki pages.
Both gateways are mocked - no network access occurs.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.plugins.builtin.parcel_buildings import (
    ParcelBuildingsEnrichmentSource,
    ParcelBuildingsPanelSource,
    ParcelBuildingsPlugin,
    building_rows,
    fetch_parcel_buildings,
    match_child_marker,
)
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

_coord_counter = 0

_REDATA_BUILDINGS = [
    {"source": "cris", "name": "Tool Shed", "building_number": "154", "year_built": 1937, "latitude": 41.73320, "longitude": -73.93040},
    {"source": "cris", "name": "Main Hall", "building_number": "9", "year_built": 1892, "latitude": 41.73300, "longitude": -73.93000},
]


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 41.733 + _coord_counter * 0.001)
    kwargs.setdefault("longitude", -73.930 - _coord_counter * 0.001)
    return baker.make(Location, google_place=None, **kwargs)


def _square_around(latitude: float, longitude: float, size: float = 0.002) -> MultiPolygon:
    """A MultiPolygon square centred on a coordinate, as a stand-in parcel boundary."""
    return MultiPolygon(
        Polygon(
            (
                (longitude - size, latitude - size),
                (longitude + size, latitude - size),
                (longitude + size, latitude + size),
                (longitude - size, latitude + size),
                (longitude - size, latitude - size),
            ),
        ),
        srid=4326,
    )


class FetchParcelBuildingsTests(TestCase):
    """REData first, Overpass only when REData has nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.location = _make_location()

    def test_redata_buildings_are_used_when_available(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=_REDATA_BUILDINGS),
        ):
            payload = fetch_parcel_buildings(self.location)
        self.assertEqual(payload["provider"], "redata")
        self.assertEqual(len(payload["buildings"]), 2)

    def test_overpass_is_not_consulted_when_redata_answers(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=_REDATA_BUILDINGS),
            patch.object(OverpassGateway, "buildings_within") as mock_overpass,
        ):
            fetch_parcel_buildings(self.location)
        mock_overpass.assert_not_called()

    def test_falls_back_to_overpass_inside_the_property_boundary(self) -> None:
        Boundary.objects.create(
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square_around(float(self.location.latitude), float(self.location.longitude)),
        )
        osm = [{"name": "Powerhouse", "building_number": "", "latitude": 41.7331, "longitude": -73.9301, "osm_id": 5, "source": "osm"}]
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value=None),
            patch.object(OverpassGateway, "__post_init__", lambda _self: None),
            patch.object(OverpassGateway, "buildings_within", return_value=osm),
        ):
            payload = fetch_parcel_buildings(self.location)
        self.assertEqual(payload["provider"], "osm")
        self.assertEqual(payload["buildings"], osm)

    def test_no_real_property_boundary_skips_overpass_entirely(self) -> None:
        """The synthesized 50m circle isn't a parcel - counting inside it would catch neighbours."""
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value=None),
            patch.object(OverpassGateway, "buildings_within") as mock_overpass,
        ):
            payload = fetch_parcel_buildings(self.location)
        mock_overpass.assert_not_called()
        self.assertEqual(payload, {})

    def test_redata_failure_falls_through_rather_than_raising(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", side_effect=PropertyRecordsUnavailableError("source_error", "boom")),
        ):
            self.assertEqual(fetch_parcel_buildings(self.location), {})

    def test_overpass_failure_is_swallowed(self) -> None:
        Boundary.objects.create(
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square_around(float(self.location.latitude), float(self.location.longitude)),
        )
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value=None),
            patch.object(OverpassGateway, "__post_init__", lambda _self: None),
            patch.object(OverpassGateway, "buildings_within", side_effect=OSError("overpass down")),
        ):
            self.assertEqual(fetch_parcel_buildings(self.location), {})


class PanelSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        self.source = ParcelBuildingsPanelSource()

    def test_gate_allows_a_root_pin_with_coordinates(self) -> None:
        self.assertTrue(self.source.gate(self.pin))

    def test_gate_rejects_a_child_pin(self) -> None:
        """A sub pin marks one building; it has no buildings of its own to list."""
        child = baker.make(Pin, profile=self.profile, location=_make_location(), parent_pin=self.pin)
        self.assertFalse(self.source.gate(child))

    def test_fetch_caches_the_building_list(self) -> None:
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=_REDATA_BUILDINGS),
        ):
            self.source.fetch(self.pin)
        cached = LocationCache.get_fresh(self.location, PARCEL_BUILDINGS_CACHE_SOURCE)
        assert cached is not None
        self.assertEqual(len(cached.data["buildings"]), 2)


class EnrichmentSourceTests(TestCase):
    def test_fetch_returns_the_payload_and_a_coordinate_query_key(self) -> None:
        location = baker.make(Location, latitude="41.733150", longitude="-73.930370", google_place=None)
        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_parcel_uuid", return_value="parcel-1"),
            patch.object(RedataGateway, "lookup_buildings", return_value=_REDATA_BUILDINGS),
        ):
            payload, query_key = ParcelBuildingsEnrichmentSource().fetch(location)
        self.assertEqual(len(payload["buildings"]), 2)
        self.assertEqual(query_key, "41.73315,-73.93037")


class MatchChildMarkerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")

    def _pin_at(self, latitude: float, longitude: float) -> Pin:
        return baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=latitude, longitude=longitude, google_place=None))

    def test_matches_a_marker_standing_on_the_building(self) -> None:
        pin = self._pin_at(41.733200, -73.930400)
        self.assertEqual(match_child_marker(_REDATA_BUILDINGS[0], [pin]), pin)

    def test_does_not_match_a_marker_at_another_building(self) -> None:
        pin = self._pin_at(41.733000, -73.930000)
        self.assertIsNone(match_child_marker(_REDATA_BUILDINGS[0], [pin]))

    def test_a_building_without_coordinates_matches_nothing(self) -> None:
        pin = self._pin_at(41.733200, -73.930400)
        self.assertIsNone(match_child_marker({"name": "Unlocated"}, [pin]))


class BuildingRowsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")

    def _pin_at(self, latitude: float, longitude: float, **kwargs) -> Pin:
        return baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(Location, latitude=latitude, longitude=longitude, google_place=None),
            **kwargs,
        )

    def test_rows_sort_numerically_by_building_number(self) -> None:
        """'Building 9' must precede 'Building 10' - the identifiers people navigate by."""
        buildings = [{"building_number": "10", "name": "Ten"}, {"building_number": "9", "name": "Nine"}, {"building_number": "100", "name": "Hundred"}]
        self.assertEqual([row["building_number"] for row in building_rows(buildings, [])], ["9", "10", "100"])

    def test_unnumbered_buildings_sort_last_by_name(self) -> None:
        buildings = [{"name": "Zed"}, {"building_number": "3", "name": "Three"}, {"name": "Alpha"}]
        self.assertEqual([row["name"] for row in building_rows(buildings, [])], ["Three", "Alpha", "Zed"])

    def test_a_matched_child_supplies_its_name_and_url(self) -> None:
        child = self._pin_at(41.733200, -73.930400, name="Tool Shed")
        rows = building_rows([_REDATA_BUILDINGS[0]], [child], url_for=lambda c: f"/pins/{c.pk}/")
        self.assertEqual(rows[0]["child_name"], "Tool Shed")
        self.assertEqual(rows[0]["child_url"], f"/pins/{child.pk}/")

    def test_an_unmatched_building_has_no_child(self) -> None:
        rows = building_rows([_REDATA_BUILDINGS[0]], [])
        self.assertEqual(rows[0]["child_name"], "")
        self.assertEqual(rows[0]["child_url"], "")

    def test_one_child_can_only_claim_one_building(self) -> None:
        """Otherwise a single pin on a dense campus would mark several footprints as done."""
        near_pair = [
            {"name": "A", "latitude": 41.73320, "longitude": -73.93040},
            {"name": "B", "latitude": 41.733205, "longitude": -73.930405},
        ]
        child = self._pin_at(41.733200, -73.930400, name="Only One")
        rows = building_rows(near_pair, [child])
        self.assertEqual(sum(1 for row in rows if row["child_name"]), 1)

    def test_source_labels_are_humanized(self) -> None:
        rows = building_rows([{"name": "X", "source": "cris"}, {"name": "Y", "source": "osm"}], [])
        self.assertEqual({row["source_label"] for row in rows}, {"NY SHPO (CRIS)", "OpenStreetMap"})

    def test_omitting_url_for_leaves_rows_unlinked(self) -> None:
        """The wiki renders the same rows, but child wikis are markers, not pages."""
        child = self._pin_at(41.733200, -73.930400, name="Tool Shed")
        rows = building_rows([_REDATA_BUILDINGS[0]], [child])
        self.assertEqual(rows[0]["child_name"], "Tool Shed")
        self.assertEqual(rows[0]["child_url"], "")


class ParcelBuildingsPanelViewTests(TestCase):
    """The pin detail page's "Buildings on this Property" endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, slug="campus")

    def _url(self) -> str:
        return reverse("pin.parcel_buildings", kwargs={"pin_slug": self.pin.slug})

    def test_empty_cache_yields_204(self) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {})
        self.assertEqual(self.client.get(self._url()).status_code, 204)

    def test_cached_buildings_render_with_their_numbers(self) -> None:
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _REDATA_BUILDINGS, "provider": "redata"})
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Tool Shed", body)
        self.assertIn("154", body)
        self.assertIn("Main Hall", body)

    def test_a_pinned_building_links_to_its_child_pin(self) -> None:
        child = baker.make(
            Pin,
            profile=self.user.profile,
            parent_pin=self.pin,
            pin_type=PinType.BUILDING,
            slug="tool-shed",
            location=baker.make(Location, latitude="41.733200", longitude="-73.930400", google_place=None),
        )
        LocationCache.set(self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": _REDATA_BUILDINGS, "provider": "redata"})
        response = self.client.get(self._url())
        self.assertContains(response, reverse("pin.details", kwargs={"pin_slug": child.slug}))

    def test_another_users_pin_is_not_reachable(self) -> None:
        other = baker.make(Pin, profile=baker.make(User).profile, location=_make_location(), slug="not-mine")
        self.assertEqual(self.client.get(reverse("pin.parcel_buildings", kwargs={"pin_slug": other.slug})).status_code, 404)


class PluginContributionsTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plugin = ParcelBuildingsPlugin()

    def test_contributes_one_panel_source(self) -> None:
        self.assertEqual([type(s) for s in self.plugin.get_panel_sources()], [ParcelBuildingsPanelSource])

    def test_contributes_one_enrichment_source(self) -> None:
        self.assertEqual([type(s) for s in self.plugin.get_enrichment_sources()], [ParcelBuildingsEnrichmentSource])


class OverpassBuildingsWithinTests(SimpleTestCase):
    """The Overpass fallback's query construction and result shaping."""

    def setUp(self) -> None:
        super().setUp()
        with patch.object(OverpassGateway, "__post_init__", lambda _self: None):
            self.gateway = OverpassGateway()

    def test_polygon_is_sent_as_lat_lon_pairs(self) -> None:
        polygon = _square_around(41.733, -73.930)
        with patch.object(OverpassGateway, "elements_for_query", return_value=[]) as mock_query:
            self.gateway.buildings_within(polygon)
        query = mock_query.call_args[0][0]
        self.assertIn('way(poly:"', query)
        self.assertIn('["building"]', query)
        # Overpass wants "lat lon", the opposite order from GeoJSON.
        self.assertIn("41.7310000 -73.9320000", query)

    def test_elements_become_building_records(self) -> None:
        elements = [{"id": 7, "center": {"lat": 41.7331, "lon": -73.9301}, "tags": {"name": "Powerhouse", "ref": "12"}}]
        with patch.object(OverpassGateway, "elements_for_query", return_value=elements):
            buildings = self.gateway.buildings_within(_square_around(41.733, -73.930))
        self.assertEqual(buildings, [{"name": "Powerhouse", "building_number": "12", "latitude": 41.7331, "longitude": -73.9301, "osm_id": 7, "source": "osm"}])

    def test_elements_without_a_centre_are_skipped(self) -> None:
        with patch.object(OverpassGateway, "elements_for_query", return_value=[{"id": 7, "tags": {"name": "No centre"}}]):
            self.assertEqual(self.gateway.buildings_within(_square_around(41.733, -73.930)), [])

    def test_untagged_elements_still_produce_a_record(self) -> None:
        with patch.object(OverpassGateway, "elements_for_query", return_value=[{"id": 7, "center": {"lat": 41.7331, "lon": -73.9301}}]):
            buildings = self.gateway.buildings_within(_square_around(41.733, -73.930))
        self.assertEqual(buildings[0]["name"], "")

    def test_an_empty_multipolygon_queries_nothing(self) -> None:
        with patch.object(OverpassGateway, "elements_for_query") as mock_query:
            self.assertEqual(self.gateway.buildings_within(MultiPolygon(srid=4326)), [])
        mock_query.assert_not_called()
