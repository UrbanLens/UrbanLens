"""Property-based tests for the map-based pin-share detection algorithm.

Covers the pure-function geometry/bearing/viewport math in
``services.map_pin_share_detection`` - no database round-trips required.
See ``test_map_pin_share_detection_integration.py`` for the DB-backed
``detect_shared_pins``/``share_markup_map_with_profile`` behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.contrib.gis.geos import Point
from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.map_pin_share_detection import (
    bearing_degrees,
    geometry_to_geos,
    is_zoomed_in,
    viewport_bounds,
)
from urbanlens.dashboard.services.map_pin_share_detection import arrow_points_toward as _arrow_points_toward
from urbanlens.dashboard.tests.hypothesis.strategies import coord_pair_float, lat_float, lon_float, two_distant_coord_pairs


def _markup_item(coordinates: list[list[float]]) -> SimpleNamespace:
    """A lightweight stand-in for a PinMarkup row - only ``.geometry`` is read."""
    return SimpleNamespace(geometry={"type": "LineString", "coordinates": coordinates})


# -- is_zoomed_in -----------------------------------------------------------------

class IsZoomedInTests(SimpleTestCase):
    def test_none_zoom_is_never_zoomed_in(self) -> None:
        self.assertFalse(is_zoomed_in(None))

    @given(st.floats(min_value=-10, max_value=100, allow_nan=False), st.floats(min_value=-10, max_value=100, allow_nan=False))
    @settings(max_examples=200)
    def test_threshold_boundary(self, zoom: float, threshold: float) -> None:
        result = is_zoomed_in(zoom, threshold=threshold)
        self.assertEqual(result, zoom >= threshold)


# -- bearing_degrees ----------------------------------------------------------------

class BearingDegreesTests(SimpleTestCase):
    @given(two_distant_coord_pairs())
    @settings(max_examples=200)
    def test_bearing_is_always_in_range(self, pair) -> None:
        (lat1, lon1), (lat2, lon2) = pair
        bearing = bearing_degrees(lat1, lon1, lat2, lon2)
        self.assertGreaterEqual(bearing, 0)
        self.assertLess(bearing, 360)

    @given(two_distant_coord_pairs())
    @settings(max_examples=200)
    def test_reverse_bearing_is_roughly_opposite(self, pair) -> None:
        """Bearing(A, B) and Bearing(B, A) should differ by ~180 degrees (mod 360).

        Only approximately true on a sphere for non-antipodal points, which
        the strategy guarantees by keeping the two points within 10 degrees
        of each other.
        """
        (lat1, lon1), (lat2, lon2) = pair
        forward = bearing_degrees(lat1, lon1, lat2, lon2)
        backward = bearing_degrees(lat2, lon2, lat1, lon1)
        diff = abs((forward - backward) % 360)
        diff = min(diff, 360 - diff)
        self.assertGreater(diff, 150)


# -- viewport_bounds ----------------------------------------------------------------

class ViewportBoundsTests(SimpleTestCase):
    @given(lat_float, lon_float, st.floats(min_value=1, max_value=20, allow_nan=False))
    @settings(max_examples=200)
    def test_center_is_always_contained(self, lat: float, lon: float, zoom: float) -> None:
        bounds = viewport_bounds(lat, lon, zoom)
        self.assertTrue(bounds.contains_point(lat, lon))

    @given(lat_float, lon_float)
    @settings(max_examples=100)
    def test_higher_zoom_shrinks_bounds(self, lat: float, lon: float) -> None:
        wide = viewport_bounds(lat, lon, 4)
        narrow = viewport_bounds(lat, lon, 16)
        self.assertLess(narrow.north - narrow.south, wide.north - wide.south)
        self.assertLess(narrow.east - narrow.west, wide.east - wide.west)

    def test_expanded_grows_symmetrically(self) -> None:
        bounds = viewport_bounds(40.0, -74.0, 14)
        expanded = bounds.expanded(5)
        self.assertLess(expanded.south, bounds.south)
        self.assertGreater(expanded.north, bounds.north)
        self.assertLess(expanded.west, bounds.west)
        self.assertGreater(expanded.east, bounds.east)


# -- geometry_to_geos ---------------------------------------------------------------

class GeometryToGeosTests(SimpleTestCase):
    def test_none_geometry_returns_none(self) -> None:
        self.assertIsNone(geometry_to_geos(None))
        self.assertIsNone(geometry_to_geos({}))

    def test_malformed_circle_returns_none(self) -> None:
        self.assertIsNone(geometry_to_geos({"type": "Circle", "coordinates": None, "radius": 50}))
        self.assertIsNone(geometry_to_geos({"type": "Circle", "coordinates": [1.0], "radius": 50}))
        self.assertIsNone(geometry_to_geos({"type": "Circle", "coordinates": [1.0, 2.0], "radius": None}))

    @given(lat_float, lon_float, st.floats(min_value=1, max_value=5000, allow_nan=False))
    @settings(max_examples=100)
    def test_circle_buffers_around_center(self, lat: float, lon: float, radius: float) -> None:
        geom = geometry_to_geos({"type": "Circle", "coordinates": [lon, lat], "radius": radius})
        self.assertIsNotNone(geom)
        self.assertTrue(geom.contains(Point(lon, lat, srid=4326)))

    @given(lat_float, lon_float)
    @settings(max_examples=100)
    def test_point_geometry_round_trips(self, lat: float, lon: float) -> None:
        geom = geometry_to_geos({"type": "Point", "coordinates": [lon, lat]})
        self.assertIsNotNone(geom)
        self.assertAlmostEqual(geom.x, lon, places=5)
        self.assertAlmostEqual(geom.y, lat, places=5)

    @given(two_distant_coord_pairs())
    @settings(max_examples=100)
    def test_linestring_geometry_round_trips(self, pair) -> None:
        (lat1, lon1), (lat2, lon2) = pair
        geom = geometry_to_geos({"type": "LineString", "coordinates": [[lon1, lat1], [lon2, lat2]]})
        self.assertIsNotNone(geom)
        self.assertEqual(len(geom.coords), 2)


# -- arrow_points_toward -------------------------------------------------------------

class ArrowPointsTowardTests(SimpleTestCase):
    @given(two_distant_coord_pairs())
    @settings(max_examples=200)
    def test_arrow_pointing_exactly_at_target_always_matches(self, pair) -> None:
        (tail_lat, tail_lon), (target_lat, target_lon) = pair
        item = _markup_item([[tail_lon, tail_lat], [target_lon, target_lat]])
        target = Point(target_lon, target_lat, srid=4326)
        self.assertTrue(_arrow_points_toward(item, target, tolerance_degrees=0.01))

    def test_arrow_pointing_away_does_not_match(self) -> None:
        # Tail at origin, head due north; target due east - ~90 degrees off.
        item = _markup_item([[0.0, 0.0], [0.0, 1.0]])
        target = Point(1.0, 0.0, srid=4326)
        self.assertFalse(_arrow_points_toward(item, target, tolerance_degrees=35))

    def test_arrow_within_tolerance_matches(self) -> None:
        # Tail at origin, head due north; target slightly north-east (within 35 degrees).
        item = _markup_item([[0.0, 0.0], [0.0, 1.0]])
        target = Point(0.3, 1.0, srid=4326)
        self.assertTrue(_arrow_points_toward(item, target, tolerance_degrees=35))

    def test_too_few_coordinates_never_matches(self) -> None:
        item = _markup_item([[0.0, 0.0]])
        target = Point(1.0, 1.0, srid=4326)
        self.assertFalse(_arrow_points_toward(item, target))
