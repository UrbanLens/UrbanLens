"""Tests for distance unit conversion/formatting and region inference."""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import DistanceUnit
from urbanlens.dashboard.models.profile.model import _units_for_point
from urbanlens.dashboard.services.units import _MILES_PER_KM, format_distance, km_to_display, unit_label
from urbanlens.dashboard.templatetags.dashboard_tags import distance as distance_filter


class KmToDisplayTests(TestCase):
    """km_to_display converts kilometres to the requested unit."""

    def test_kilometers_passthrough(self) -> None:
        self.assertEqual(km_to_display(12.5, DistanceUnit.KILOMETERS), 12.5)

    def test_miles_conversion(self) -> None:
        self.assertAlmostEqual(km_to_display(10.0, DistanceUnit.MILES), 10.0 * _MILES_PER_KM, places=6)

    @given(st.floats(min_value=0, max_value=1_000_000, allow_nan=False, allow_infinity=False))
    def test_miles_are_shorter_than_km_for_positive_distance(self, distance_km: float) -> None:
        miles = km_to_display(distance_km, DistanceUnit.MILES)
        self.assertLessEqual(miles, distance_km + 1e-9)


class UnitLabelTests(TestCase):
    """unit_label returns the short display label."""

    def test_labels(self) -> None:
        self.assertEqual(unit_label(DistanceUnit.KILOMETERS), "km")
        self.assertEqual(unit_label(DistanceUnit.MILES), "mi")


class FormatDistanceTests(TestCase):
    """format_distance renders value + unit label."""

    def test_km(self) -> None:
        self.assertEqual(format_distance(12.34, DistanceUnit.KILOMETERS), "12.3 km")

    def test_miles(self) -> None:
        self.assertEqual(format_distance(10.0, DistanceUnit.MILES), "6.2 mi")

    def test_decimals_arg(self) -> None:
        self.assertEqual(format_distance(12.345, DistanceUnit.KILOMETERS, decimals=2), "12.34 km")


class UnitsForPointTests(TestCase):
    """_units_for_point picks miles inside miles-using regions, km otherwise."""

    def test_continental_us_is_miles(self) -> None:
        self.assertEqual(_units_for_point(40.71, -74.0), DistanceUnit.MILES)  # New York

    def test_united_kingdom_is_miles(self) -> None:
        self.assertEqual(_units_for_point(51.5, -0.12), DistanceUnit.MILES)  # London

    def test_france_is_kilometers(self) -> None:
        self.assertEqual(_units_for_point(48.85, 2.35), DistanceUnit.KILOMETERS)  # Paris

    def test_japan_is_kilometers(self) -> None:
        self.assertEqual(_units_for_point(35.68, 139.69), DistanceUnit.KILOMETERS)  # Tokyo

    def test_mid_ocean_is_kilometers(self) -> None:
        self.assertEqual(_units_for_point(0.0, -30.0), DistanceUnit.KILOMETERS)


class DistanceFilterTests(TestCase):
    """The `distance` template filter formats km values in the given unit."""

    def test_km(self) -> None:
        self.assertEqual(distance_filter(12.34, DistanceUnit.KILOMETERS), "12.3 km")

    def test_miles(self) -> None:
        self.assertEqual(distance_filter(10.0, DistanceUnit.MILES), "6.2 mi")

    def test_defaults_to_km(self) -> None:
        self.assertEqual(distance_filter(5.0), "5.0 km")

    def test_none_is_empty_string(self) -> None:
        self.assertEqual(distance_filter(None, DistanceUnit.KILOMETERS), "")

    def test_non_numeric_is_empty_string(self) -> None:
        self.assertEqual(distance_filter("abc", DistanceUnit.MILES), "")
