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
  - CUSTOM / AUTO modes always set gps_fallback_lat/lng to None.
  - map_pins_meta returns null when the profile has no pins, and an ISO
    timestamp equal to the most-recently-updated pin's timestamp otherwise.
"""
from __future__ import annotations

import decimal
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import MapCenterMode, Profile
from urbanlens.UrbanLens.settings.app import settings as app_settings

_db_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_MAP_URL = "/dashboard/map/"
_MAP_META_URL = "/dashboard/map/pins/meta/"


def _pin_at(profile: Profile, lat: float, lng: float, **kwargs) -> Pin:
    """Create a Pin whose linked Location sits at the given coordinates."""
    location = baker.make(Location, latitude=lat, longitude=lng)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


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
        parent = baker.make(Pin, profile=self.profile, parent_pin=None)
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
        _pin_at(self.profile, 40.7, -74.0)
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
        _pin_at(self.profile, 40.7, -74.0)
        for mode in (MapCenterMode.AUTO, MapCenterMode.CUSTOM):
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

    @given(n=st.integers(min_value=0, max_value=6))
    @_db_settings
    def test_pin_count_equals_root_pin_count_for_n_pins(self, n: int) -> None:
        # hypothesis.extra.django flushes the DB session between examples via
        # _pre_setup/_post_teardown even though setUp data survives in the outer
        # class transaction.  Re-login here so each example has a valid session.
        self.client.force_login(self.user)
        for _ in range(n):
            baker.make(Pin, profile=self.profile, parent_pin=None)
        resp = self.client.get(_MAP_URL)
        self.assertEqual(resp.context["pin_count"], n)


class ShowPinCountTests(TestCase):
    """Total pin count on the map is visible only to site admins in development."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.site_settings import EnvironmentOverrideChoice, SiteSettings
        from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group

        self._SiteSettings = SiteSettings
        self._dev = EnvironmentOverrideChoice.DEVELOPMENT
        self._prod = EnvironmentOverrideChoice.PRODUCTION
        self._add_admin = add_user_to_site_admin_group

    def test_hidden_for_regular_user_in_development(self) -> None:
        baker.make(User)  # bootstrap admin
        user: User = baker.make(User)
        self._SiteSettings.objects.filter(pk=1).update(environment_override=self._dev)
        self.client.force_login(user)
        # Independent of the local .env's UL_ALLOW_DEV_TOOLBAR_FOR_NON_ADMINS.
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=False):
            resp = self.client.get(_MAP_URL)
        self.assertFalse(resp.context["show_pin_count"])

    def test_hidden_for_site_admin_in_production(self) -> None:
        user: User = baker.make(User)
        self._add_admin(user)
        self._SiteSettings.objects.filter(pk=1).update(environment_override=self._prod)
        self.client.force_login(user)
        resp = self.client.get(_MAP_URL)
        self.assertFalse(resp.context["show_pin_count"])

    def test_visible_for_site_admin_in_development(self) -> None:
        user: User = baker.make(User)
        self._add_admin(user)
        self._SiteSettings.objects.filter(pk=1).update(environment_override=self._dev)
        self.client.force_login(user)
        resp = self.client.get(_MAP_URL)
        self.assertTrue(resp.context["show_pin_count"])

    def test_visible_for_superuser_in_development(self) -> None:
        user: User = baker.make(User, is_superuser=True)
        self._SiteSettings.objects.filter(pk=1).update(environment_override=self._dev)
        self.client.force_login(user)
        resp = self.client.get(_MAP_URL)
        self.assertTrue(resp.context["show_pin_count"])


# -- map_pins_meta -------------------------------------------------------------

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
