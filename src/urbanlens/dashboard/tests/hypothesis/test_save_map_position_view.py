"""Tests for SaveMapPositionView (POST /settings/map-position/).

Server-side confirmation for UL-255 ("remember last map position doesn't
work") - locks in that the write side is correctly implemented and gated, so
a future investigation doesn't re-suspect it. See docs/PROBLEMS.md for the
more likely actual cause: a separate, unrelated shareable-map-view-URL
feature on the map page takes precedence over the server-remembered value
on page load, independent of anything tested here.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile


class SaveMapPositionViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, **overrides) -> dict:
        data = {"lat": "42.65", "lng": "-73.75", "zoom": "12"}
        data.update(overrides)
        return self.client.post(reverse("settings.save_map_position"), data)

    def test_saves_position_when_in_remember_mode(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertAlmostEqual(float(self.profile.remembered_map_lat), 42.65, places=4)
        self.assertAlmostEqual(float(self.profile.remembered_map_lng), -73.75, places=4)
        self.assertEqual(self.profile.remembered_map_zoom, 12)

    def test_ignores_request_when_not_in_remember_mode(self) -> None:
        """The view's own docstring: "ignores the request silently otherwise
        so stale JS calls are harmless" - confirms that contract."""
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.GPS)

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False, "reason": "not in remember mode"})
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.remembered_map_lat)

    def test_out_of_range_latitude_is_rejected(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        response = self._post(lat="91")
        self.assertEqual(response.status_code, 400)

    def test_out_of_range_zoom_is_rejected(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        response = self._post(zoom="23")
        self.assertEqual(response.status_code, 400)

    def test_malformed_values_are_rejected(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        response = self._post(lat="not-a-number")
        self.assertEqual(response.status_code, 400)

    def test_missing_fields_are_rejected(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        response = self.client.post(reverse("settings.save_map_position"), {"lat": "42.65"})
        self.assertEqual(response.status_code, 400)

    def test_repeated_saves_overwrite_the_previous_position(self) -> None:
        """Confirms this is a live "last known position", not append-only -
        the mechanism the page's debounced JS relies on."""
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        self._post(lat="10", lng="20", zoom="5")
        self._post(lat="30", lng="40", zoom="8")

        self.profile.refresh_from_db()
        self.assertAlmostEqual(float(self.profile.remembered_map_lat), 30.0, places=4)
        self.assertAlmostEqual(float(self.profile.remembered_map_lng), 40.0, places=4)
        self.assertEqual(self.profile.remembered_map_zoom, 8)

    def test_saved_position_is_reflected_on_the_next_map_load(self) -> None:
        """End-to-end within what the server controls: a saved position feeds
        straight back into view_map's rendered context on the next request -
        the piece that's demonstrably NOT broken server-side."""
        Profile.objects.filter(pk=self.profile.pk).update(map_center_mode=MapCenterMode.REMEMBER)
        self._post(lat="15.5", lng="-25.5", zoom="9")

        body = self.client.get(reverse("map.view")).content.decode()
        self.assertIn("_SERVER_CENTER_LAT = 15.5", body)
        self.assertIn("_MAP_CENTER_MODE   = 'remember'", body)
