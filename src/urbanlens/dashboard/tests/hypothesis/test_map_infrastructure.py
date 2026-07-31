"""Tests for the main map's active and historic Water & Rail overlay."""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
import pytest

from urbanlens.dashboard.controllers.maps import MapController
from urbanlens.dashboard.services import infrastructure_map
from urbanlens.dashboard.templatetags.map_components import MAP_LAYER_REGISTRY


class InfrastructureBoundsTests(SimpleTestCase):
    def test_parses_leaflet_bbox_order(self) -> None:
        bounds = infrastructure_map.parse_infrastructure_bbox("-73.9,42.6,-73.7,42.8")
        self.assertEqual((bounds.west, bounds.south, bounds.east, bounds.north), (-73.9, 42.6, -73.7, 42.8))
        self.assertEqual(bounds.overpass_bbox, "42.60000,-73.90000,42.80000,-73.70000")

    def test_rejects_missing_reversed_and_country_sized_bounds(self) -> None:
        for value in (None, "", "1,2,3", "west,2,3,4", "4,2,3,5", "-75,40,-70,45"):
            with self.subTest(value=value), pytest.raises(ValueError, match="bbox"):
                infrastructure_map.parse_infrastructure_bbox(value)

    def test_query_includes_active_and_historic_route_tags(self) -> None:
        bounds = infrastructure_map.InfrastructureBounds(west=-73.9, south=42.6, east=-73.7, north=42.8)
        query = infrastructure_map.build_infrastructure_query(bounds)
        self.assertIn('way["railway"', query)
        self.assertIn('way["disused:railway"]', query)
        self.assertIn('way["abandoned:railway"]', query)
        self.assertIn('way["demolished:railway"]', query)
        self.assertIn('way["railtrail"="yes"]', query)
        self.assertIn('way["waterway"', query)
        self.assertIn('way["disused:waterway"]', query)
        self.assertIn('way["abandoned:waterway"]', query)
        self.assertIn('way["demolished:waterway"]', query)
        self.assertIn('way["historic"~"^(railway|canal)$"]', query)
        self.assertIn("(42.60000,-73.90000,42.80000,-73.70000)", query)


class InfrastructureFeatureCollectionTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.bounds = infrastructure_map.InfrastructureBounds(west=-73.9, south=42.6, east=-73.7, north=42.8)

    @mock.patch.object(infrastructure_map, "OverpassGateway")
    def test_builds_styled_geojson_properties_and_skips_invalid_geometry(self, gateway_class: mock.Mock) -> None:
        gateway_class.return_value.elements_for_query.return_value = [
            {
                "type": "way",
                "id": 10,
                "tags": {"name": "Main Line", "railway": "rail"},
                "geometry": [{"lat": 42.6, "lon": -73.9}, {"lat": 42.7, "lon": -73.8}],
            },
            {
                "type": "way",
                "id": 11,
                "tags": {"name": "Old Towpath", "railtrail": "yes"},
                "geometry": [{"lat": 42.61, "lon": -73.89}, {"lat": 42.71, "lon": -73.79}],
            },
            {
                "type": "way",
                "id": 12,
                "tags": {"waterway": "derelict_canal"},
                "geometry": [{"lat": 42.62, "lon": -73.88}, {"lat": 42.72, "lon": -73.78}],
            },
            {"type": "way", "id": 13, "tags": {"railway": "rail"}, "geometry": [{"lat": 42.6, "lon": -73.9}]},
        ]

        collection = infrastructure_map.infrastructure_feature_collection(self.bounds)

        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 3)
        active_rail, rail_trail, canal = collection["features"]
        self.assertEqual(active_rail["properties"]["kind"], "rail")
        self.assertFalse(active_rail["properties"]["historic"])
        self.assertEqual(active_rail["geometry"]["coordinates"][0], [-73.9, 42.6])
        self.assertTrue(rail_trail["properties"]["historic"])
        self.assertEqual(rail_trail["properties"]["type"], "Rail Trail")
        self.assertEqual(canal["properties"]["kind"], "water")
        self.assertTrue(canal["properties"]["historic"])
        self.assertEqual(canal["properties"]["name"], "Historic Derelict Canal")
        self.assertEqual(canal["properties"]["osm_url"], "https://www.openstreetmap.org/way/12")

    @mock.patch.object(infrastructure_map, "OverpassGateway")
    def test_reuses_cached_viewport(self, gateway_class: mock.Mock) -> None:
        gateway_class.return_value.elements_for_query.return_value = []
        infrastructure_map.infrastructure_feature_collection(self.bounds)
        infrastructure_map.infrastructure_feature_collection(self.bounds)
        gateway_class.return_value.elements_for_query.assert_called_once()


class InfrastructureMapEndpointTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.url = reverse("map.infrastructure")

    def test_requires_authentication(self) -> None:
        request = self.factory.get(self.url, {"bbox": "-73.9,42.6,-73.7,42.8"})
        request.user = AnonymousUser()
        response = MapController.as_view({"get": "infrastructure_features"})(request)
        self.assertIn(response.status_code, (301, 302))

    def test_rejects_oversized_viewport(self) -> None:
        request = self.factory.get(self.url, {"bbox": "-75,40,-70,45"})
        response = MapController().infrastructure_features(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("zoom in", json.loads(response.content)["error"])

    @mock.patch(
        "urbanlens.dashboard.services.infrastructure_map.infrastructure_feature_collection",
        return_value={"type": "FeatureCollection", "features": [], "attribution": "© OpenStreetMap contributors"},
    )
    def test_returns_geojson_with_private_browser_cache(self, feature_collection: mock.Mock) -> None:
        request = self.factory.get(self.url, {"bbox": "-73.9,42.6,-73.7,42.8"})
        response = MapController().infrastructure_features(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["type"], "FeatureCollection")
        self.assertEqual(response["Cache-Control"], "private, max-age=300")
        feature_collection.assert_called_once()

    def test_layer_registry_exposes_water_and_rail_toggle(self) -> None:
        layer = MAP_LAYER_REGISTRY["infrastructure"]
        self.assertEqual(layer.label, "Water & Rail")
        self.assertEqual(layer.kind, "custom")
