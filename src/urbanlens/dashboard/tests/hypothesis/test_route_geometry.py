"""Tests for services.import_formats.route_geometry.simplify_and_measure().

Pure-function tests - no models/DB involved, since simplify_and_measure only
deals with plain point lists and returns a GEOS LineString.
"""
from __future__ import annotations

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure

_hyp = hyp_settings(max_examples=50, deadline=None)

_lat = st.floats(min_value=-80, max_value=80, allow_nan=False, allow_infinity=False)
_lng = st.floats(min_value=-170, max_value=170, allow_nan=False, allow_infinity=False)
_points = st.lists(st.tuples(_lat, _lng), min_size=2, max_size=25)


class SimplifyAndMeasureTests(SimpleTestCase):
    """simplify_and_measure() simplifies a point sequence and measures its true distance."""

    def test_raises_for_fewer_than_two_points(self):
        with self.assertRaises(ValueError):
            simplify_and_measure([(1.0, 2.0)])

    def test_raises_for_empty_list(self):
        with self.assertRaises(ValueError):
            simplify_and_measure([])

    def test_two_points_are_never_simplified_further(self):
        result = simplify_and_measure([(1.0, 2.0), (1.001, 2.001)])
        self.assertEqual(result.raw_point_count, 2)
        self.assertEqual(result.simplified_point_count, 2)
        self.assertGreater(result.distance_meters, 0)

    @_hyp
    @given(points=_points)
    def test_simplification_never_grows_point_count(self, points: list[tuple[float, float]]):
        result = simplify_and_measure(points)
        self.assertEqual(result.raw_point_count, len(points))
        self.assertLessEqual(result.simplified_point_count, result.raw_point_count)
        self.assertGreaterEqual(result.simplified_point_count, 2)

    @_hyp
    @given(points=_points)
    def test_distance_is_non_negative(self, points: list[tuple[float, float]]):
        result = simplify_and_measure(points)
        self.assertGreaterEqual(result.distance_meters, 0)

    @_hyp
    @given(points=_points)
    def test_simplified_path_preserves_endpoints(self, points: list[tuple[float, float]]):
        result = simplify_and_measure(points)
        first_lng, first_lat = result.path.coords[0]
        last_lng, last_lat = result.path.coords[-1]
        self.assertAlmostEqual(first_lat, points[0][0], places=5)
        self.assertAlmostEqual(first_lng, points[0][1], places=5)
        self.assertAlmostEqual(last_lat, points[-1][0], places=5)
        self.assertAlmostEqual(last_lng, points[-1][1], places=5)

    def test_straight_line_distance_matches_sum_of_segments(self):
        # Three colinear-ish points a known distance apart (roughly 111km per
        # degree of latitude at the equator) - a coarse sanity check that
        # distance is being summed across all segments, not just endpoints.
        points = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]
        result = simplify_and_measure(points)
        self.assertGreater(result.distance_meters, 100_000)
        self.assertLess(result.distance_meters, 130_000)
