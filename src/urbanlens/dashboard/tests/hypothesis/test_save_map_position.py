"""Tests for saving the remembered map position from the map UI."""

from __future__ import annotations

import decimal

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile

_SAVE_MAP_POSITION_URL = "/dashboard/settings/map-position/"


class SaveMapPositionAuthTests(TestCase):
	"""The remembered-map endpoint is only available to authenticated users."""

	def test_unauthenticated_request_redirects(self) -> None:
		resp = self.client.post(_SAVE_MAP_POSITION_URL, {"lat": "42.65", "lng": "-73.75", "zoom": "10"})
		self.assertIn(resp.status_code, (301, 302))


class SaveMapPositionTests(TestCase):
	"""SaveMapPositionView persists valid map state only in REMEMBER mode."""

	user: User
	profile: Profile

	def setUp(self) -> None:
		super().setUp()
		self.user = baker.make(User)
		self.profile = self.user.profile
		self.client.force_login(self.user)

	def test_persists_position_when_profile_is_in_remember_mode(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)

		resp = self.client.post(_SAVE_MAP_POSITION_URL, {"lat": "42.650001", "lng": "-73.750002", "zoom": "12"})

		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.json(), {"ok": True})
		self.profile.refresh_from_db()
		self.assertAlmostEqual(float(self.profile.remembered_map_lat), 42.650001, places=6)
		self.assertAlmostEqual(float(self.profile.remembered_map_lng), -73.750002, places=6)
		self.assertEqual(self.profile.remembered_map_zoom, 12)

	def test_non_remember_mode_does_not_update_saved_position(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.GPS,
			remembered_map_lat=decimal.Decimal("1.000000"),
			remembered_map_lng=decimal.Decimal("2.000000"),
			remembered_map_zoom=3,
		)

		resp = self.client.post(_SAVE_MAP_POSITION_URL, {"lat": "42.65", "lng": "-73.75", "zoom": "12"})

		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.json(), {"ok": False, "reason": "not in remember mode"})
		self.profile.refresh_from_db()
		self.assertEqual(self.profile.remembered_map_lat, decimal.Decimal("1.000000"))
		self.assertEqual(self.profile.remembered_map_lng, decimal.Decimal("2.000000"))
		self.assertEqual(self.profile.remembered_map_zoom, 3)

	def test_missing_coordinate_payload_is_rejected(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)

		resp = self.client.post(_SAVE_MAP_POSITION_URL, {"lat": "42.65", "zoom": "12"})

		self.assertEqual(resp.status_code, 400)
		self.assertEqual(resp.json(), {"error": "lat, lng, zoom required"})

	def test_out_of_range_payload_is_rejected_without_updating_profile(self) -> None:
		Profile.objects.filter(pk=self.profile.pk).update(
			map_center_mode=MapCenterMode.REMEMBER,
			remembered_map_lat=decimal.Decimal("1.000000"),
			remembered_map_lng=decimal.Decimal("2.000000"),
			remembered_map_zoom=3,
		)
		invalid_payloads = [
			{"lat": "90.1", "lng": "-73.75", "zoom": "12"},
			{"lat": "42.65", "lng": "-180.1", "zoom": "12"},
			{"lat": "42.65", "lng": "-73.75", "zoom": "23"},
		]

		for payload in invalid_payloads:
			with self.subTest(payload=payload):
				resp = self.client.post(_SAVE_MAP_POSITION_URL, payload)
				self.assertEqual(resp.status_code, 400)
				self.assertEqual(resp.json(), {"error": "out of range"})
				self.profile.refresh_from_db()
				self.assertEqual(self.profile.remembered_map_lat, decimal.Decimal("1.000000"))
				self.assertEqual(self.profile.remembered_map_lng, decimal.Decimal("2.000000"))
				self.assertEqual(self.profile.remembered_map_zoom, 3)
