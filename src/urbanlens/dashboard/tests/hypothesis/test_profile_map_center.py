"""Property-based and unit tests for Profile.get_map_center and compute_map_center.

Invariants verified:
  - GPS mode always returns None regardless of stored coordinates.
  - CUSTOM mode returns stored coordinates as floats, or None when either is unset.
  - AUTO mode returns the cached centroid without touching the DB; falls back to
    compute_map_center() when the cache is empty.
  - compute_map_center() finds the densest cluster of pins and returns its centroid,
    rather than a naive average across all pins (which would land in the ocean for
    users with pins on multiple continents).
  - When all pins are nearby, the cluster centroid equals their geographic midpoint.
  - When pins are spread across continents, the largest regional cluster wins.
  - The result is written to the DB cache and returned as (float, float).
  - Pins with no usable coordinates produce None from compute_map_center().
"""
from __future__ import annotations

import decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import _CLUSTER_RADIUS_KM, MapCenterMode, Profile, _haversine_km
from urbanlens.dashboard.tests.hypothesis.strategies import latitude, longitude, valid_zoom

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _pin_at(profile: Profile, lat: float, lng: float, **kwargs) -> Pin:
    """Create a Pin whose linked Location sits at the given coordinates.

    A Pin no longer stores its own coordinates; they live on the shared Location
    it references (see AddressableModel), so tests that care about a pin's map
    position must create a Location at those coordinates.
    """
    location = baker.make(Location, latitude=lat, longitude=lng)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


def _profile_with_mode(mode: str, **extra) -> Profile:
    profile: Profile = baker.make(User).profile
    Profile.objects.filter(pk=profile.pk).update(map_center_mode=mode, **extra)
    profile.refresh_from_db()
    return profile


# -- GPS mode ------------------------------------------------------------------

class GetMapCenterGpsModeTests(TestCase):
    """GPS mode must always return None - the browser handles geolocation."""

    def test_gps_mode_returns_none_with_no_stored_coords(self) -> None:
        profile = _profile_with_mode(MapCenterMode.GPS)
        self.assertIsNone(profile.get_map_center())

    def test_gps_mode_returns_none_even_when_custom_coords_are_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.GPS,
            map_custom_latitude=decimal.Decimal("42.65"),
            map_custom_longitude=decimal.Decimal("-73.75"),
        )
        self.assertIsNone(profile.get_map_center())

    def test_gps_mode_returns_none_even_when_cached_centroid_is_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.GPS,
            map_center_latitude=decimal.Decimal("42.65"),
            map_center_longitude=decimal.Decimal("-73.75"),
        )
        self.assertIsNone(profile.get_map_center())

    @given(lat=latitude, lng=longitude)
    @_db_settings
    def test_gps_mode_returns_none_for_any_stored_custom_coords(
        self, lat: decimal.Decimal, lng: decimal.Decimal
    ) -> None:
        profile = _profile_with_mode(
            MapCenterMode.GPS,
            map_custom_latitude=lat,
            map_custom_longitude=lng,
        )
        self.assertIsNone(profile.get_map_center())


# -- CUSTOM mode ---------------------------------------------------------------

class GetMapCenterCustomModeTests(TestCase):
    """CUSTOM mode returns the stored coordinates, or None when either is missing."""

    def test_custom_mode_returns_tuple_when_both_coords_are_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.CUSTOM,
            map_custom_latitude=decimal.Decimal("42.650000"),
            map_custom_longitude=decimal.Decimal("-73.750000"),
        )
        result = profile.get_map_center()
        self.assertIsNotNone(result)
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result[0], 42.65, places=4)
        self.assertAlmostEqual(result[1], -73.75, places=4)

    def test_custom_mode_returns_floats_not_decimals(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.CUSTOM,
            map_custom_latitude=decimal.Decimal("10.000000"),
            map_custom_longitude=decimal.Decimal("20.000000"),
        )
        result = profile.get_map_center()
        assert result is not None  # nosec B101
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], float)

    def test_custom_mode_returns_none_when_only_latitude_is_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.CUSTOM,
            map_custom_latitude=decimal.Decimal("42.65"),
            map_custom_longitude=None,
        )
        self.assertIsNone(profile.get_map_center())

    def test_custom_mode_returns_none_when_only_longitude_is_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.CUSTOM,
            map_custom_latitude=None,
            map_custom_longitude=decimal.Decimal("-73.75"),
        )
        self.assertIsNone(profile.get_map_center())

    def test_custom_mode_returns_none_when_neither_coord_is_set(self) -> None:
        profile = _profile_with_mode(MapCenterMode.CUSTOM)
        self.assertIsNone(profile.get_map_center())

    @given(lat=latitude, lng=longitude)
    @_db_settings
    def test_custom_mode_returns_stored_values_exactly(
        self, lat: decimal.Decimal, lng: decimal.Decimal
    ) -> None:
        profile = _profile_with_mode(
            MapCenterMode.CUSTOM,
            map_custom_latitude=lat,
            map_custom_longitude=lng,
        )
        result = profile.get_map_center()
        self.assertIsNotNone(result)
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result[0], float(lat), places=5)
        self.assertAlmostEqual(result[1], float(lng), places=5)


class GetMapCenterRememberModeTests(TestCase):
    """REMEMBER mode returns the stored remembered_map_lat/lng, or None when unset.

    Server-side confirmation for UL-255 ("remember last map position doesn't
    work") - the read side (this), the write side (SaveMapPositionView, see
    test_save_map_position_view.py), MapCenterForm.save(), and the map page's
    JS are all independently correct; see docs/PROBLEMS.md for the more
    likely actual cause (a separate, unrelated shareable-map-view-URL feature
    taking precedence over the server-rendered value on page load).
    """

    def test_remember_mode_returns_tuple_when_both_coords_are_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.REMEMBER,
            remembered_map_lat=decimal.Decimal("42.650000"),
            remembered_map_lng=decimal.Decimal("-73.750000"),
        )
        result = profile.get_map_center()
        self.assertIsNotNone(result)
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result[0], 42.65, places=4)
        self.assertAlmostEqual(result[1], -73.75, places=4)

    def test_remember_mode_returns_floats_not_decimals(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.REMEMBER,
            remembered_map_lat=decimal.Decimal("10.000000"),
            remembered_map_lng=decimal.Decimal("20.000000"),
        )
        result = profile.get_map_center()
        assert result is not None  # nosec B101
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], float)

    def test_remember_mode_returns_none_when_never_saved(self) -> None:
        profile = _profile_with_mode(MapCenterMode.REMEMBER)
        self.assertIsNone(profile.get_map_center())

    def test_remember_mode_returns_none_when_only_latitude_is_set(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.REMEMBER,
            remembered_map_lat=decimal.Decimal("42.65"),
            remembered_map_lng=None,
        )
        self.assertIsNone(profile.get_map_center())

    def test_remember_mode_template_context_reflects_stored_value(self) -> None:
        """view_map renders **profile.get_map_center_template_context() directly -
        this is what actually reaches the page's `_SERVER_CENTER_LAT` JS constant."""
        profile = _profile_with_mode(
            MapCenterMode.REMEMBER,
            remembered_map_lat=decimal.Decimal("42.650000"),
            remembered_map_lng=decimal.Decimal("-73.750000"),
        )
        context = profile.get_map_center_template_context()
        self.assertEqual(context["map_center_mode"], MapCenterMode.REMEMBER)
        self.assertAlmostEqual(context["map_center_lat"], 42.65, places=4)
        self.assertAlmostEqual(context["map_center_lng"], -73.75, places=4)


# -- AUTO mode - cached centroid -----------------------------------------------

class GetMapCenterAutoCachedTests(TestCase):
    """AUTO mode returns the cached centroid without hitting the DB for pins."""

    def test_auto_mode_returns_cached_centroid(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.AUTO,
            map_center_latitude=decimal.Decimal("42.650000"),
            map_center_longitude=decimal.Decimal("-73.750000"),
        )
        result = profile.get_map_center()
        self.assertIsNotNone(result)
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result[0], 42.65, places=4)
        self.assertAlmostEqual(result[1], -73.75, places=4)

    def test_auto_mode_cached_centroid_returned_as_floats(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.AUTO,
            map_center_latitude=decimal.Decimal("10.000000"),
            map_center_longitude=decimal.Decimal("20.000000"),
        )
        result = profile.get_map_center()
        assert result is not None  # nosec B101
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], float)

    def test_auto_mode_does_not_call_compute_when_cache_is_warm(self) -> None:
        profile = _profile_with_mode(
            MapCenterMode.AUTO,
            map_center_latitude=decimal.Decimal("42.650000"),
            map_center_longitude=decimal.Decimal("-73.750000"),
        )
        with patch.object(profile, "compute_map_center") as mock_compute:
            profile.get_map_center()
        mock_compute.assert_not_called()


# -- AUTO mode - cold cache ----------------------------------------------------

class GetMapCenterAutoColdTests(TestCase):
    """AUTO mode falls back to compute_map_center() when the cache is empty."""

    def test_auto_mode_returns_none_when_no_cache_and_no_pins(self) -> None:
        profile = _profile_with_mode(MapCenterMode.AUTO)
        self.assertIsNone(profile.get_map_center())

    def test_auto_mode_calls_compute_when_cache_is_empty(self) -> None:
        profile = _profile_with_mode(MapCenterMode.AUTO)
        with patch.object(profile, "compute_map_center", return_value=None) as mock_compute:
            profile.get_map_center()
        mock_compute.assert_called_once()

    def test_auto_mode_returns_computed_centroid_when_pins_exist(self) -> None:
        profile = _profile_with_mode(MapCenterMode.AUTO)
        location = baker.make(Location, latitude=40.0, longitude=-74.0)
        baker.make(Pin, profile=profile, location=location)
        result = profile.get_map_center()
        self.assertIsNotNone(result)
        assert result is not None  # nosec B101
        self.assertAlmostEqual(result[0], 40.0, places=2)
        self.assertAlmostEqual(result[1], -74.0, places=2)


# -- compute_map_center --------------------------------------------------------

class ComputeMapCenterTests(TestCase):
    """compute_map_center() averages pin coordinates and persists the result."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_returns_none_when_profile_has_no_pins(self) -> None:
        self.assertIsNone(self.profile.compute_map_center())

    def test_single_pin_centroid_equals_pin_coordinates(self) -> None:
        _pin_at(self.profile, 42.65, -73.75)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 42.65, places=4)
        self.assertAlmostEqual(result[1], -73.75, places=4)

    def test_two_nearby_pins_result_is_their_midpoint(self) -> None:
        # These two points are ~140 km apart - both in the same cluster.
        _pin_at(self.profile, 40.0, -70.0)
        _pin_at(self.profile, 41.0, -71.0)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 40.5, places=3)
        self.assertAlmostEqual(result[1], -70.5, places=3)

    def test_result_is_cached_on_profile_in_db(self) -> None:
        _pin_at(self.profile, 42.65, -73.75)
        self.profile.compute_map_center()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)
        self.assertIsNotNone(self.profile.map_center_longitude)
        self.assertAlmostEqual(float(self.profile.map_center_latitude), 42.65, places=4)

    def test_result_is_also_set_on_instance(self) -> None:
        _pin_at(self.profile, 42.65, -73.75)
        self.profile.compute_map_center()
        # No refresh_from_db - check the in-memory instance
        self.assertIsNotNone(self.profile.map_center_latitude)

    def test_returns_floats_not_decimals(self) -> None:
        _pin_at(self.profile, 42.65, -73.75)
        result = self.profile.compute_map_center()
        self.assertIsInstance(result[0], float)
        self.assertIsInstance(result[1], float)

    def test_falls_back_to_location_coordinates_when_pin_has_no_override(self) -> None:
        location = baker.make(Location, latitude=50.0, longitude=10.0)
        # A Pin reads its coordinates from the linked Location.
        baker.make(Pin, profile=self.profile, location=location)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 50.0, places=2)
        self.assertAlmostEqual(result[1], 10.0, places=2)

    @given(
        lat1=st.floats(min_value=-80.0, max_value=78.0, allow_nan=False, allow_infinity=False),
        dlat=st.floats(min_value=0.01, max_value=2.0, allow_nan=False, allow_infinity=False),
        lng1=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
        dlng=st.floats(min_value=0.01, max_value=2.0, allow_nan=False, allow_infinity=False),
    )
    @_db_settings
    def test_two_nearby_pins_result_is_their_midpoint_hypothesis(
        self, lat1: float, dlat: float, lng1: float, dlng: float,
    ) -> None:
        """For two pins that are close together, the cluster centroid equals their midpoint."""
        lat2, lng2 = lat1 + dlat, lng1 + dlng
        assume(_haversine_km((lat1, lng1), (lat2, lng2)) <= _CLUSTER_RADIUS_KM)
        _pin_at(self.profile, lat1, lng1)
        _pin_at(self.profile, lat2, lng2)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], (lat1 + lat2) / 2, places=4)
        self.assertAlmostEqual(result[1], (lng1 + lng2) / 2, places=4)


# -- Clustering behaviour ------------------------------------------------------

class ComputeMapCenterClusteringTests(TestCase):
    """The largest geographic cluster wins over intercontinental spreads."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_larger_cluster_wins_over_isolated_pin(self) -> None:
        # Three pins near NYC (~40°N 74°W) vs one pin near London (51°N 0°W).
        # The NYC cluster has more members and must win.
        nyc = [(40.7, -74.0), (40.8, -73.9), (40.6, -74.1)]
        for lat, lng in nyc:
            _pin_at(self.profile, lat, lng)
        _pin_at(self.profile, 51.5, -0.1)  # London
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        # Result must be near NYC, not in the middle of the Atlantic.
        self.assertGreater(result[0], 35.0)   # latitude well above equator
        self.assertLess(result[0], 50.0)      # but not near London
        self.assertLess(result[1], -40.0)     # longitude clearly in the Americas

    def test_equal_sized_clusters_returns_a_cluster_centroid_not_midpoint(self) -> None:
        # One pin in NYC and one in London.  The result must be one of the two
        # locations - NOT the midpoint in the mid-Atlantic (~46°N 37°W).
        _pin_at(self.profile, 40.7, -74.0)   # NYC
        _pin_at(self.profile, 51.5, -0.1)    # London
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        near_nyc = abs(result[0] - 40.7) < 1.0 and abs(result[1] - -74.0) < 1.0
        near_london = abs(result[0] - 51.5) < 1.0 and abs(result[1] - -0.1) < 1.0
        self.assertTrue(
            near_nyc or near_london,
            f"Result {result} should be near NYC or London, not mid-Atlantic",
        )

    def test_result_is_within_the_winning_cluster_bounding_box(self) -> None:
        # Six pins in Europe, two pins in South America.  Result must be in Europe.
        europe = [(48.8, 2.3), (51.5, -0.1), (52.5, 13.4), (41.9, 12.5), (40.4, -3.7), (50.0, 14.4)]
        for lat, lng in europe:
            _pin_at(self.profile, lat, lng)
        for lat, lng in [(-23.5, -46.6), (-34.6, -58.4)]:
            _pin_at(self.profile, lat, lng)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        # Result must be in the European latitude/longitude band.
        self.assertGreater(result[0], 30.0)   # north of Africa
        self.assertLess(result[0], 60.0)      # south of Scandinavia
        self.assertGreater(result[1], -10.0)  # east of Atlantic
        self.assertLess(result[1], 25.0)      # west of Turkey

    @given(n=st.integers(min_value=1, max_value=8))
    @_db_settings
    def test_result_is_always_within_bounding_box_of_all_pins(self, n: int) -> None:
        """The cluster centroid must never fall outside the geographic extent of all pins."""
        lats = [40.0 + i * 0.1 for i in range(n)]
        lngs = [-74.0 - i * 0.1 for i in range(n)]
        for lat, lng in zip(lats, lngs):
            _pin_at(self.profile, lat, lng)
        result = self.profile.compute_map_center()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result[0], min(lats) - 0.001)
        self.assertLessEqual(result[0], max(lats) + 0.001)
        self.assertGreaterEqual(result[1], min(lngs) - 0.001)
        self.assertLessEqual(result[1], max(lngs) + 0.001)


# -- map_default_zoom default --------------------------------------------------

class MapDefaultZoomTests(TestCase):
    """map_default_zoom defaults to 13 for new profiles."""

    def test_new_profile_has_default_zoom_of_13(self) -> None:
        profile: Profile = baker.make(User).profile
        self.assertEqual(profile.map_default_zoom, 13)

    @given(zoom=valid_zoom)
    @_db_settings
    def test_stored_zoom_is_returned_unchanged(self, zoom: int) -> None:
        profile: Profile = baker.make(User).profile
        Profile.objects.filter(pk=profile.pk).update(map_default_zoom=zoom)
        profile.refresh_from_db()
        self.assertEqual(profile.map_default_zoom, zoom)
