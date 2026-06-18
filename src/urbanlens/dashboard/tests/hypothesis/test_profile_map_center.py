"""Property-based and unit tests for Profile.get_map_center and compute_map_center.

Invariants verified:
  - GPS mode always returns None regardless of stored coordinates.
  - CUSTOM mode returns stored coordinates as floats, or None when either is unset.
  - AUTO mode returns the cached centroid without touching the DB; falls back to
    compute_map_center() when the cache is empty.
  - compute_map_center() averages pin coordinates (falling back to location coords
    via Coalesce), writes the result to the DB cache, and returns floats.
  - Pins with no usable coordinates produce None from compute_map_center().
"""
from __future__ import annotations

import decimal
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase
from model_bakery import baker

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile
from urbanlens.dashboard.tests.hypothesis.strategies import latitude, longitude, valid_zoom

_DB_SETTINGS = dict(
	max_examples=30,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _profile_with_mode(mode: str, **extra) -> Profile:
	profile = baker.make("auth.User").profile
	Profile.objects.filter(pk=profile.pk).update(map_center_mode=mode, **extra)
	profile.refresh_from_db()
	return profile


# ── GPS mode ──────────────────────────────────────────────────────────────────

class GetMapCenterGpsModeTests(HypothesisTestCase):
	"""GPS mode must always return None — the browser handles geolocation."""

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
	@settings(**_DB_SETTINGS)
	def test_gps_mode_returns_none_for_any_stored_custom_coords(
		self, lat: decimal.Decimal, lng: decimal.Decimal
	) -> None:
		profile = _profile_with_mode(
			MapCenterMode.GPS,
			map_custom_latitude=lat,
			map_custom_longitude=lng,
		)
		self.assertIsNone(profile.get_map_center())


# ── CUSTOM mode ───────────────────────────────────────────────────────────────

class GetMapCenterCustomModeTests(HypothesisTestCase):
	"""CUSTOM mode returns the stored coordinates, or None when either is missing."""

	def test_custom_mode_returns_tuple_when_both_coords_are_set(self) -> None:
		profile = _profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude=decimal.Decimal("42.650000"),
			map_custom_longitude=decimal.Decimal("-73.750000"),
		)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 42.65, places=4)
		self.assertAlmostEqual(result[1], -73.75, places=4)

	def test_custom_mode_returns_floats_not_decimals(self) -> None:
		profile = _profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude=decimal.Decimal("10.000000"),
			map_custom_longitude=decimal.Decimal("20.000000"),
		)
		result = profile.get_map_center()
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
	@settings(**_DB_SETTINGS)
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
		self.assertAlmostEqual(result[0], float(lat), places=5)
		self.assertAlmostEqual(result[1], float(lng), places=5)


# ── AUTO mode — cached centroid ───────────────────────────────────────────────

class GetMapCenterAutoCachedTests(HypothesisTestCase):
	"""AUTO mode returns the cached centroid without hitting the DB for pins."""

	def test_auto_mode_returns_cached_centroid(self) -> None:
		profile = _profile_with_mode(
			MapCenterMode.AUTO,
			map_center_latitude=decimal.Decimal("42.650000"),
			map_center_longitude=decimal.Decimal("-73.750000"),
		)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 42.65, places=4)
		self.assertAlmostEqual(result[1], -73.75, places=4)

	def test_auto_mode_cached_centroid_returned_as_floats(self) -> None:
		profile = _profile_with_mode(
			MapCenterMode.AUTO,
			map_center_latitude=decimal.Decimal("10.000000"),
			map_center_longitude=decimal.Decimal("20.000000"),
		)
		result = profile.get_map_center()
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


# ── AUTO mode — cold cache ────────────────────────────────────────────────────

class GetMapCenterAutoColdTests(HypothesisTestCase):
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
		baker.make(Pin, profile=profile, location=location, latitude=40.0, longitude=-74.0)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 40.0, places=2)
		self.assertAlmostEqual(result[1], -74.0, places=2)


# ── compute_map_center ────────────────────────────────────────────────────────

class ComputeMapCenterTests(HypothesisTestCase):
	"""compute_map_center() averages pin coordinates and persists the result."""

	def setUp(self) -> None:
		super().setUp()
		self.profile = baker.make("auth.User").profile

	def test_returns_none_when_profile_has_no_pins(self) -> None:
		self.assertIsNone(self.profile.compute_map_center())

	def test_single_pin_centroid_equals_pin_coordinates(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=42.65, longitude=-73.75)
		result = self.profile.compute_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 42.65, places=4)
		self.assertAlmostEqual(result[1], -73.75, places=4)

	def test_two_pins_centroid_is_their_midpoint(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=40.0, longitude=-70.0)
		baker.make(Pin, profile=self.profile, latitude=44.0, longitude=-78.0)
		result = self.profile.compute_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 42.0, places=3)
		self.assertAlmostEqual(result[1], -74.0, places=3)

	def test_result_is_cached_on_profile_in_db(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=42.65, longitude=-73.75)
		self.profile.compute_map_center()
		self.profile.refresh_from_db()
		self.assertIsNotNone(self.profile.map_center_latitude)
		self.assertIsNotNone(self.profile.map_center_longitude)
		self.assertAlmostEqual(float(self.profile.map_center_latitude), 42.65, places=4)

	def test_result_is_also_set_on_instance(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=42.65, longitude=-73.75)
		self.profile.compute_map_center()
		# No refresh_from_db — check the in-memory instance
		self.assertIsNotNone(self.profile.map_center_latitude)

	def test_returns_floats_not_decimals(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=42.65, longitude=-73.75)
		result = self.profile.compute_map_center()
		self.assertIsInstance(result[0], float)
		self.assertIsInstance(result[1], float)

	def test_falls_back_to_location_coordinates_when_pin_has_no_override(self) -> None:
		location = baker.make(Location, latitude=50.0, longitude=10.0)
		# Pin has no coordinate override — Coalesce must use location coords.
		baker.make(Pin, profile=self.profile, location=location, latitude=None, longitude=None)
		result = self.profile.compute_map_center()
		self.assertIsNotNone(result)
		self.assertAlmostEqual(result[0], 50.0, places=2)
		self.assertAlmostEqual(result[1], 10.0, places=2)

	@given(
		lat1=st.floats(min_value=-80.0, max_value=80.0, allow_nan=False, allow_infinity=False),
		lat2=st.floats(min_value=-80.0, max_value=80.0, allow_nan=False, allow_infinity=False),
		lng1=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
		lng2=st.floats(min_value=-170.0, max_value=170.0, allow_nan=False, allow_infinity=False),
	)
	@settings(**_DB_SETTINGS)
	def test_centroid_latitude_is_between_input_latitudes(
		self, lat1: float, lat2: float, lng1: float, lng2: float
	) -> None:
		baker.make(Pin, profile=self.profile, latitude=lat1, longitude=lng1)
		baker.make(Pin, profile=self.profile, latitude=lat2, longitude=lng2)
		result = self.profile.compute_map_center()
		self.assertIsNotNone(result)
		self.assertGreaterEqual(result[0], min(lat1, lat2) - 0.001)
		self.assertLessEqual(result[0], max(lat1, lat2) + 0.001)


# ── map_default_zoom default ──────────────────────────────────────────────────

class MapDefaultZoomTests(HypothesisTestCase):
	"""map_default_zoom defaults to 13 for new profiles."""

	def test_new_profile_has_default_zoom_of_13(self) -> None:
		profile = baker.make("auth.User").profile
		self.assertEqual(profile.map_default_zoom, 13)

	@given(zoom=valid_zoom)
	@settings(**_DB_SETTINGS)
	def test_stored_zoom_is_returned_unchanged(self, zoom: int) -> None:
		profile = baker.make("auth.User").profile
		Profile.objects.filter(pk=profile.pk).update(map_default_zoom=zoom)
		profile.refresh_from_db()
		self.assertEqual(profile.map_default_zoom, zoom)
