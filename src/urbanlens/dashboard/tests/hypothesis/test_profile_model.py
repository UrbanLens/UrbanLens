"""Tests for Profile model properties and helper functions.

Pure-function tests use unittest.TestCase (no DB).
DB-backed tests use django.test.TestCase with baker.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import (
	MapCenterMode,
	MapViewChoice,
	Profile,
	VisibilityChoice,
	_haversine_km,
)
from urbanlens.dashboard.tests.hypothesis.strategies import lat_float, lon_float


_hyp = settings(max_examples=100, deadline=None)
_hyp_db = settings(max_examples=20, deadline=None)


# ── _haversine_km ─────────────────────────────────────────────────────────────

class HaversineTests(TestCase):
	"""_haversine_km computes great-circle distances in kilometres."""

	def test_same_point_returns_zero(self) -> None:
		self.assertAlmostEqual(_haversine_km((40.0, -74.0), (40.0, -74.0)), 0.0, places=5)

	def test_known_distance_nyc_to_london(self) -> None:
		# New York (~40.71, -74.00) to London (~51.51, -0.13) ≈ 5,570 km
		nyc = (40.7128, -74.0060)
		london = (51.5074, -0.1278)
		dist = _haversine_km(nyc, london)
		self.assertGreater(dist, 5500)
		self.assertLess(dist, 5650)

	def test_north_pole_to_equator_is_roughly_quarter_earth(self) -> None:
		# For R=6371: π/2 * 6371 ≈ 10007.54 km
		dist = _haversine_km((90.0, 0.0), (0.0, 0.0))
		self.assertAlmostEqual(dist, 10007.5, delta=5.0)

	def test_equator_half_circumference(self) -> None:
		# For R=6371: π * 6371 ≈ 20015.09 km
		dist = _haversine_km((0.0, 0.0), (0.0, 180.0))
		self.assertAlmostEqual(dist, 20015.0, delta=5.0)

	def test_returns_float(self) -> None:
		result = _haversine_km((0.0, 0.0), (1.0, 1.0))
		self.assertIsInstance(result, float)

	@given(lat_float, lon_float)
	@_hyp
	def test_self_distance_is_zero(self, lat: float, lon: float) -> None:
		dist = _haversine_km((lat, lon), (lat, lon))
		self.assertAlmostEqual(dist, 0.0, places=5)

	@given(lat_float, lon_float, lat_float, lon_float)
	@_hyp
	def test_non_negative(self, lat1: float, lon1: float, lat2: float, lon2: float) -> None:
		self.assertGreaterEqual(_haversine_km((lat1, lon1), (lat2, lon2)), 0.0)

	@given(lat_float, lon_float, lat_float, lon_float)
	@_hyp
	def test_symmetric(self, lat1: float, lon1: float, lat2: float, lon2: float) -> None:
		d1 = _haversine_km((lat1, lon1), (lat2, lon2))
		d2 = _haversine_km((lat2, lon2), (lat1, lon1))
		self.assertAlmostEqual(d1, d2, places=5)

	@given(lat_float, lon_float, lat_float, lon_float, lat_float, lon_float)
	@settings(max_examples=50, deadline=None)
	def test_triangle_inequality(
		self,
		lat1: float, lon1: float,
		lat2: float, lon2: float,
		lat3: float, lon3: float,
	) -> None:
		d12 = _haversine_km((lat1, lon1), (lat2, lon2))
		d23 = _haversine_km((lat2, lon2), (lat3, lon3))
		d13 = _haversine_km((lat1, lon1), (lat3, lon3))
		# Allow a small floating-point tolerance.
		self.assertLessEqual(d13, d12 + d23 + 1e-6)

	@given(lat_float, lon_float, lat_float, lon_float)
	@_hyp
	def test_bounded_by_half_earth_circumference(
		self, lat1: float, lon1: float, lat2: float, lon2: float
	) -> None:
		# Maximum distance on Earth ≈ 20,038 km
		dist = _haversine_km((lat1, lon1), (lat2, lon2))
		self.assertLessEqual(dist, 20040.0)


# ── Profile proxy properties ──────────────────────────────────────────────────

class ProfileProxyPropertyTests(HypothesisTestCase):
	"""Profile proxies user.username, email, first_name, last_name, full_name."""

	def _make_user(self, username="testuser", first="First", last="Last", email="a@b.com"):
		user = baker.make("auth.User", username=username, first_name=first, last_name=last, email=email)
		return user

	def test_username_proxies_to_user(self) -> None:
		user = self._make_user(username="myuser")
		self.assertEqual(user.profile.username, "myuser")

	def test_email_proxies_to_user(self) -> None:
		user = self._make_user(email="test@example.com")
		self.assertEqual(user.profile.email, "test@example.com")

	def test_first_name_proxies_to_user(self) -> None:
		user = self._make_user(first="Alice")
		self.assertEqual(user.profile.first_name, "Alice")

	def test_last_name_proxies_to_user(self) -> None:
		user = self._make_user(last="Smith")
		self.assertEqual(user.profile.last_name, "Smith")

	def test_full_name_is_space_joined_first_and_last(self) -> None:
		user = self._make_user(first="John", last="Doe")
		self.assertEqual(user.profile.full_name, "John Doe")

	def test_str_returns_username(self) -> None:
		user = self._make_user(username="theuser")
		self.assertEqual(str(user.profile), "theuser")


# ── Profile.get_map_center ────────────────────────────────────────────────────

class ProfileGetMapCenterTests(HypothesisTestCase):
	"""get_map_center() returns coordinates based on the mode setting."""

	def _profile_with_mode(self, mode: str, **extra) -> Profile:
		user: User = baker.make(User)
		Profile.objects.filter(pk=user.profile.pk).update(map_center_mode=mode, **extra)
		user.profile.refresh_from_db()
		return user.profile

	def test_gps_mode_returns_none(self) -> None:
		profile = self._profile_with_mode(MapCenterMode.GPS)
		self.assertIsNone(profile.get_map_center())

	def test_custom_mode_with_coords_returns_them(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude="42.500000",
			map_custom_longitude="-73.500000",
		)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		assert result is not None
		self.assertAlmostEqual(result[0], 42.5, places=3)
		self.assertAlmostEqual(result[1], -73.5, places=3)

	def test_custom_mode_without_coords_returns_none(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude=None,
			map_custom_longitude=None,
		)
		self.assertIsNone(profile.get_map_center())

	def test_auto_mode_with_cached_coords_returns_them(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.AUTO,
			map_center_latitude="40.000000",
			map_center_longitude="-75.000000",
		)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		assert result is not None
		self.assertAlmostEqual(result[0], 40.0, places=3)
		self.assertAlmostEqual(result[1], -75.0, places=3)

	def test_auto_mode_without_cached_coords_calls_compute(self) -> None:
		# With no pins, compute_map_center returns None.
		profile = self._profile_with_mode(
			MapCenterMode.AUTO,
			map_center_latitude=None,
			map_center_longitude=None,
		)
		result = profile.get_map_center()
		self.assertIsNone(result)

	def test_get_map_center_returns_float_tuple_when_coords_set(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude="51.500000",
			map_custom_longitude="-0.120000",
		)
		result = profile.get_map_center()
		self.assertIsNotNone(result)
		assert result is not None
		self.assertIsInstance(result[0], float)
		self.assertIsInstance(result[1], float)


# ── Profile.compute_map_center ────────────────────────────────────────────────

class ProfileComputeMapCenterTests(HypothesisTestCase):
	"""compute_map_center() returns the centroid of the user's pins."""

	def test_no_pins_returns_none(self) -> None:
		user: User = baker.make(User)
		result = user.profile.compute_map_center()
		self.assertIsNone(result)

	def test_single_pin_returns_its_coords(self) -> None:
		user: User = baker.make(User)
		location: Location = baker.make(Location, latitude="41.000000", longitude="-74.000000")
		baker.make("dashboard.Pin", profile=user.profile, location=location, latitude=None, longitude=None)
		result = user.profile.compute_map_center()
		self.assertIsNotNone(result)
		assert result is not None
		self.assertAlmostEqual(result[0], 41.0, places=2)
		self.assertAlmostEqual(result[1], -74.0, places=2)

	def test_two_nearby_pins_centroid_is_between_them(self) -> None:
		user: User = baker.make(User)
		loc1: Location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
		loc2: Location = baker.make(Location, latitude="42.000000", longitude="-72.000000")
		baker.make("dashboard.Pin", profile=user.profile, location=loc1, latitude=None, longitude=None)
		baker.make("dashboard.Pin", profile=user.profile, location=loc2, latitude=None, longitude=None)
		result = user.profile.compute_map_center()
		self.assertIsNotNone(result)
		lat, lng = result
		self.assertGreater(lat, 40.0)
		self.assertLess(lat, 42.0)
		self.assertGreater(lng, -74.0)
		self.assertLess(lng, -72.0)

	def test_result_is_cached_on_profile(self) -> None:
		user: User = baker.make(User)
		location: Location = baker.make(Location, latitude="39.000000", longitude="-77.000000")
		baker.make("dashboard.Pin", profile=user.profile, location=location, latitude=None, longitude=None)
		user.profile.compute_map_center()
		user.profile.refresh_from_db()
		self.assertIsNotNone(user.profile.map_center_latitude)
		self.assertIsNotNone(user.profile.map_center_longitude)

	def test_intercontinental_pins_picks_largest_cluster(self) -> None:
		# Four pins in Europe and one in North America — the European cluster should win.
		user: User = baker.make(User)
		europe_coords = [
			("48.850000", "2.350000"),    # Paris
			("51.500000", "-0.120000"),   # London
			("52.370000", "4.890000"),    # Amsterdam
			("48.200000", "16.370000"),   # Vienna
		]
		for lat, lng in europe_coords:
			loc = baker.make(Location, latitude=lat, longitude=lng)
			baker.make("dashboard.Pin", profile=user.profile, location=loc, latitude=None, longitude=None)
		# One outlier in North America.
		na_loc = baker.make(Location, latitude="40.710000", longitude="-74.000000")
		baker.make("dashboard.Pin", profile=user.profile, location=na_loc, latitude=None, longitude=None)

		result = user.profile.compute_map_center()
		self.assertIsNotNone(result)
		lat, lng = result
		# European cluster centroid should be in Europe (positive longitude, lat ~48-52).
		self.assertGreater(lat, 40.0)
		self.assertLess(lat, 60.0)
		# Longitude should be in Europe, not near -74.
		self.assertGreater(lng, -10.0)
		self.assertLess(lng, 20.0)

	def test_pin_with_coordinate_override_uses_override(self) -> None:
		# Pin.latitude/longitude override the location coords in compute_map_center.
		user: User = baker.make(User)
		loc = baker.make(Location, latitude="0.000000", longitude="0.000000")
		baker.make(
			"dashboard.Pin",
			profile=user.profile,
			location=loc,
			latitude="50.000000",
			longitude="10.000000",
		)
		result = user.profile.compute_map_center()
		self.assertIsNotNone(result)
		lat, lng = result
		self.assertAlmostEqual(lat, 50.0, places=1)
		self.assertAlmostEqual(lng, 10.0, places=1)

	def test_compute_map_center_returns_tuple_of_two_floats(self) -> None:
		user: User = baker.make(User)
		loc = baker.make(Location, latitude="45.000000", longitude="9.000000")
		baker.make("dashboard.Pin", profile=user.profile, location=loc, latitude=None, longitude=None)
		result = user.profile.compute_map_center()
		self.assertIsNotNone(result)
		self.assertIsInstance(result, tuple)
		self.assertEqual(len(result), 2)
		self.assertIsInstance(result[0], float)
		self.assertIsInstance(result[1], float)


# ── VisibilityChoice / MapViewChoice / MapCenterMode ──────────────────────────

class ProfileChoiceTests(TestCase):
	"""Choice enumerations have expected values."""

	def test_visibility_choice_includes_anyone(self) -> None:
		self.assertIn("anyone", VisibilityChoice.values)

	def test_visibility_choice_includes_friends(self) -> None:
		self.assertIn("friends", VisibilityChoice.values)

	def test_visibility_choice_includes_no_one(self) -> None:
		self.assertIn("no_one", VisibilityChoice.values)

	def test_map_view_choice_includes_satellite(self) -> None:
		self.assertIn("satellite", MapViewChoice.values)

	def test_map_view_choice_includes_street(self) -> None:
		self.assertIn("street", MapViewChoice.values)

	def test_map_view_choice_includes_topographic(self) -> None:
		self.assertIn("topographic", MapViewChoice.values)

	def test_map_center_mode_includes_auto(self) -> None:
		self.assertIn("auto", MapCenterMode.values)

	def test_map_center_mode_includes_gps(self) -> None:
		self.assertIn("gps", MapCenterMode.values)

	def test_map_center_mode_includes_custom(self) -> None:
		self.assertIn("custom", MapCenterMode.values)


# ── Profile.get_map_center – custom mode with only one coord None ─────────────

class ProfileGetMapCenterEdgeCaseTests(TestCase):
	"""get_map_center() edge: CUSTOM mode with only one coordinate set returns None."""

	def _profile_with_mode(self, mode, **extra) -> Profile:
		user = baker.make(User)
		Profile.objects.filter(pk=user.profile.pk).update(map_center_mode=mode, **extra)
		user.profile.refresh_from_db()
		return user.profile

	def test_custom_mode_lat_set_but_lng_none_returns_none(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude="48.000000",
			map_custom_longitude=None,
		)
		self.assertIsNone(profile.get_map_center())

	def test_custom_mode_lng_set_but_lat_none_returns_none(self) -> None:
		profile = self._profile_with_mode(
			MapCenterMode.CUSTOM,
			map_custom_latitude=None,
			map_custom_longitude="2.000000",
		)
		self.assertIsNone(profile.get_map_center())
