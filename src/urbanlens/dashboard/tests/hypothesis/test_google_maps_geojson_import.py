"""Tests for GoogleMapsGateway.geojson_to_dict() - GeoJSON pin extraction.

This method (renamed from ``takeout_json_to_dict``) handles two shapes:

- Google Takeout's "Saved Places" export: ``Point`` geometry, ``name``/
  ``description``/``address`` properties. This is the pre-existing behavior
  and must be unaffected by the broadening below.
- Generic GeoJSON (Overpass Turbo exports, custom scripts): arbitrary geometry
  types (reduced to a centroid) and arbitrary property names (via the
  ``pick_name_and_description`` fallback heuristic).
"""
from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

_hyp = hyp_settings(max_examples=40, deadline=None)
_SAMPLE_DATA_DIR = Path(__file__).resolve().parents[5] / "sample_data"


def _feature_collection(features: list[dict]) -> str:
    return json.dumps({"type": "FeatureCollection", "features": features})


class GeojsonToDictTakeoutShapeTests(SimpleTestCase):
    """Existing Google Takeout "Saved Places" behavior is preserved exactly."""

    def setUp(self):
        self.gateway = GoogleMapsGateway(api_key="test-key")
        self.profile = object()

    def test_point_with_name_description_address(self):
        text = _feature_collection(
            [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-73.9744011, 40.7262740]},
                    "properties": {"name": "Old Factory", "description": "Ruins", "address": "123 Main St"},
                },
            ],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Old Factory")
        self.assertIn("Ruins", pins[0]["description"])
        self.assertIn("123 Main St", pins[0]["description"])
        self.assertAlmostEqual(pins[0]["latitude"], 40.7262740)
        self.assertAlmostEqual(pins[0]["longitude"], -73.9744011)

    def test_missing_name_defaults_to_unknown_location(self):
        text = _feature_collection(
            [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}, "properties": {}}],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(pins[0]["name"], "Unknown Location")

    def test_non_point_coordinates_length_still_skips_gracefully(self):
        text = _feature_collection(
            [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0]}, "properties": {"name": "Bad"}}],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(pins, [])


class GeojsonToDictGenericShapeTests(SimpleTestCase):
    """Broadened support for arbitrary GeoJSON (Overpass/OSM exports, custom scripts)."""

    def setUp(self):
        self.gateway = GoogleMapsGateway(api_key="test-key")
        self.profile = object()

    def test_polygon_reduced_to_centroid(self):
        text = _feature_collection(
            [
                {
                    "type": "Feature",
                    "properties": {"@id": "way/1", "abandoned": "yes", "building": "yes"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]]],
                    },
                },
            ],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], 1.0)
        self.assertAlmostEqual(pins[0]["latitude"], 1.0)
        self.assertIn("abandoned", pins[0]["description"])

    def test_linestring_reduced_to_centroid(self):
        text = _feature_collection(
            [
                {
                    "type": "Feature",
                    "properties": {"highway": "path"},
                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [2, 0]]},
                },
            ],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], 1.0)
        self.assertAlmostEqual(pins[0]["latitude"], 0.0)

    def test_generic_properties_without_name_key_use_fallback(self):
        # Modeled on the real USGS earthquake GeoJSON feed, which has no "name"
        # key at all - only "place"/"title".
        text = _feature_collection(
            [
                {
                    "type": "Feature",
                    "properties": {"mag": 3.2, "place": "19 km W of Point MacKenzie", "title": "M 3.2 - 19 km W"},
                    "geometry": {"type": "Point", "coordinates": [-150.346, 61.376]},
                },
            ],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "19 km W of Point MacKenzie")

    def test_unsupported_geometry_type_skipped(self):
        text = _feature_collection(
            [{"type": "Feature", "properties": {"name": "Bad"}, "geometry": {"type": "Circle", "coordinates": []}}],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(pins, [])

    def test_real_world_sample_file(self):
        sample_text = (_SAMPLE_DATA_DIR / "sample.geojson").read_text(encoding="utf-8")

        pins = self.gateway.geojson_to_dict(sample_text, self.profile)

        # 3 real USGS earthquake points + 1 real building Polygon + 1 LineString.
        self.assertEqual(len(pins), 5)
        for pin in pins:
            self.assertIsInstance(pin["latitude"], float)
            self.assertIsInstance(pin["longitude"], float)

    @_hyp
    @given(
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_point_coordinates(self, lon: float, lat: float):
        text = _feature_collection(
            [{"type": "Feature", "properties": {"name": "Spot"}, "geometry": {"type": "Point", "coordinates": [lon, lat]}}],
        )

        pins = self.gateway.geojson_to_dict(text, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=6)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=6)
