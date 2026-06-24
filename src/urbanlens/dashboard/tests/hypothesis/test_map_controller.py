"""Tests for MapController.view_map and MapController.map_pins_meta.

Invariants verified:
  - view_map requires authentication; unauthenticated requests are redirected.
  - view_map populates key context variables: pin_count, use_pin_cache,
    map_center_mode, and map_default_zoom from the user's profile.
  - pin_count in context equals the real number of root pins for the profile.
  - GPS mode sets map_center_lat/lng to None in the context, but populates
    gps_fallback_lat/lng with the pin-cluster centroid when pins exist.
  - GPS mode sets gps_fallback_lat/lng to None when the profile has no pins.
  - CUSTOM mode with stored coordinates sets map_center_lat/lng correctly.
  - REMEMBER mode with stored coordinates sets map_center_lat/lng and uses remembered zoom.
  - CUSTOM / AUTO / REMEMBER modes always set gps_fallback_lat/lng to None.
  - map_pins_meta returns null when the profile has no pins, and an ISO
    timestamp equal to the most-recently-updated pin's timestamp otherwise.
"""
from __future__ import annotations

import decimal
import json

from django.contrib.auth.models import User
from django.urls import reverse
from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile

_db_settings = settings(
	max_examples=20,
	deadline=None,
	suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_MAP_URL      = "/dashboard/map/"
_MAP_META_URL = "/dashboard/map/pins/meta/"


class ViewMapAuthTests(TestCase):
	"""view_map must redirect anonymous users to the login page."""

	def test_unauthenticated_request_redirects(self) -> None:
		resp = self.client.get(_MAP_URL)
		self.assertIn(resp.status_code, (301, 302))

	def test_authenticated_request_returns_200(self) -> None:
		user: User = baker.make(User)
		self.client.force_login(user)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.status_code, 200)


class ViewMapContextTests(TestCase):
	"""view_map must include correct values for profile-driven context variables."""

	user: User

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make(User)
		self.profile = self.user.profile
		self.client.force_login(self.user)

	def test_pin_count_is_zero_when_no_pins(self) -> None:
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["pin_count"], 0)

	def test_pin_count_reflects_actual_root_pin_count(self) -> None:
		for _ in range(3):
			baker.make(Pin, profile=self.profile)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["pin_count"], 3)

	def test_pin_count_excludes_child_pins(self) -> None:
		parent = baker.make(Pin, profile=self.profile, parent_pin=None, parent_location=None)
		baker.make(Pin, profile=self.profile, parent_pin=parent)  # child pin
		resp = self.client.get(_MAP_URL)
		# Only the root pin counts.
		self.assertEqual(resp.context["pin_count"], 1)

	def test_use_pin_cache_true_from_profile(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(use_pin_cache=True)
		resp = self.client.get(_MAP_URL)
		self.assertTrue(resp.context["use_pin_cache"])

	def test_use_pin_cache_false_from_profile(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(use_pin_cache=False)
		resp = self.client.get(_MAP_URL)
		self.assertFalse(resp.context["use_pin_cache"])

	def test_map_center_mode_is_in_context(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.GPS)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["map_center_mode"], MapCenterMode.GPS)

	def test_map_default_zoom_is_in_context(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_default_zoom=10)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["map_default_zoom"], 10)

	def test_gps_mode_sets_map_center_lat_to_none(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.GPS)
		resp = self.client.get(_MAP_URL)
		self.assertIsNone(resp.context["map_center_lat"])
		self.assertIsNone(resp.context["map_center_lng"])

	def test_gps_mode_with_no_pins_sets_gps_fallback_to_none(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.GPS)
		resp = self.client.get(_MAP_URL)
		self.assertIsNone(resp.context["gps_fallback_lat"])
		self.assertIsNone(resp.context["gps_fallback_lng"])

	def test_gps_mode_with_pins_provides_gps_fallback(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.GPS)
		baker.make(Pin, profile=self.profile, latitude=40.7, longitude=-74.0)
		resp = self.client.get(_MAP_URL)
		self.assertIsNotNone(resp.context["gps_fallback_lat"])
		self.assertIsNotNone(resp.context["gps_fallback_lng"])
		self.assertAlmostEqual(resp.context["gps_fallback_lat"], 40.7, places=2)
		self.assertAlmostEqual(resp.context["gps_fallback_lng"], -74.0, places=2)

	def test_gps_mode_uses_cached_centroid_without_recomputing(self) -> None:
		"""If map_center_latitude is already cached, the controller should not recompute."""
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.GPS,
			map_center_latitude=decimal.Decimal("51.5"),
			map_center_longitude=decimal.Decimal("-0.1"),
		)
		resp = self.client.get(_MAP_URL)
		self.assertAlmostEqual(resp.context["gps_fallback_lat"], 51.5, places=2)
		self.assertAlmostEqual(resp.context["gps_fallback_lng"], -0.1, places=2)

	def test_non_gps_mode_does_not_set_gps_fallback(self) -> None:
		baker.make(Pin, profile=self.profile, latitude=40.7, longitude=-74.0)
		for mode in (MapCenterMode.AUTO, MapCenterMode.CUSTOM, MapCenterMode.REMEMBER):
			with self.subTest(mode=mode):
				Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=mode)
				resp = self.client.get(_MAP_URL)
				self.assertIsNone(resp.context["gps_fallback_lat"])
				self.assertIsNone(resp.context["gps_fallback_lng"])

	def test_custom_mode_with_coords_sets_map_center_in_context(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.CUSTOM,
			map_custom_latitude=decimal.Decimal("42.650000"),
			map_custom_longitude=decimal.Decimal("-73.750000"),
		)
		resp = self.client.get(_MAP_URL)
		self.assertAlmostEqual(resp.context["map_center_lat"], 42.65, places=4)
		self.assertAlmostEqual(resp.context["map_center_lng"], -73.75, places=4)

	def test_custom_mode_without_coords_sets_map_center_to_none(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.CUSTOM,
			map_custom_latitude=None,
			map_custom_longitude=None,
		)
		resp = self.client.get(_MAP_URL)
		self.assertIsNone(resp.context["map_center_lat"])
		self.assertIsNone(resp.context["map_center_lng"])

	def test_remember_mode_with_coords_sets_map_center_in_context(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.REMEMBER,
			remembered_map_lat=decimal.Decimal("42.650000"),
			remembered_map_lng=decimal.Decimal("-73.750000"),
		)
		resp = self.client.get(_MAP_URL)
		self.assertAlmostEqual(resp.context["map_center_lat"], 42.65, places=4)
		self.assertAlmostEqual(resp.context["map_center_lng"], -73.75, places=4)

	def test_remember_mode_uses_remembered_zoom_in_context(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.REMEMBER,
			map_default_zoom=13,
			remembered_map_zoom=8,
		)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["map_default_zoom"], 8)

	def test_remember_mode_without_remembered_zoom_uses_default_zoom(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.REMEMBER,
			map_default_zoom=11,
			remembered_map_zoom=None,
		)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["map_default_zoom"], 11)

	@given(n=st.integers(min_value=0, max_value=6))
	@_db_settings
	def test_pin_count_equals_root_pin_count_for_n_pins(self, n: int) -> None:
		# hypothesis.extra.django flushes the DB session between examples via
		# _pre_setup/_post_teardown even though setUp data survives in the outer
		# class transaction.  Re-login here so each example has a valid session.
		self.client.force_login(self.user)
		for _ in range(n):
			baker.make(Pin, profile=self.profile, parent_pin=None, parent_location=None)
		resp = self.client.get(_MAP_URL)
		self.assertEqual(resp.context["pin_count"], n)


# ── map_pins_meta ─────────────────────────────────────────────────────────────

class MapPinsMetaTests(TestCase):
	"""map_pins_meta must return the latest pin update timestamp or null."""

	user: User

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make(User)
		self.client.force_login(self.user)

	def test_returns_null_last_updated_when_no_pins(self) -> None:
		resp = self.client.get(_MAP_META_URL)
		self.assertEqual(resp.status_code, 200)
		data = json.loads(resp.content)
		self.assertIn("last_updated", data)
		self.assertIsNone(data["last_updated"])

	def test_returns_iso_timestamp_when_pins_exist(self) -> None:
		baker.make(Pin, profile=self.user.profile)
		resp = self.client.get(_MAP_META_URL)
		self.assertEqual(resp.status_code, 200)
		data = json.loads(resp.content)
		self.assertIsNotNone(data["last_updated"])
		# Must be parseable as ISO 8601.
		from datetime import datetime
		datetime.fromisoformat(data["last_updated"])

	def test_timestamp_matches_most_recently_updated_pin(self) -> None:
		profile = self.user.profile
		pin = baker.make(Pin, profile=profile)
		resp = self.client.get(_MAP_META_URL)
		data = json.loads(resp.content)
		self.assertIsNotNone(data["last_updated"])
		# Re-fetch pin to get the exact updated timestamp Django stored.
		pin.refresh_from_db()
		self.assertEqual(data["last_updated"], pin.updated.isoformat())

	def test_unauthenticated_request_is_redirected(self) -> None:
		self.client.logout()
		resp = self.client.get(_MAP_META_URL)
		self.assertIn(resp.status_code, (301, 302))
