"""Tests for LocationCache staleness and the shared, Location-scoped caching contract."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.site_settings.model import SiteSettings

_hyp = hyp_settings(max_examples=30, deadline=None)


class LocationCacheStalenessTests(TestCase):
    """is_stale() uses the site's configured external_data_cache_days as its threshold."""

    def _make_entry(self, *, age_days: float) -> LocationCache:
        location = baker.make(Location, latitude=40.0, longitude=-74.0)
        entry = LocationCache.set(location, "wikipedia", {"summary": "test"})
        LocationCache.objects.filter(pk=entry.pk).update(updated=timezone.now() - timedelta(days=age_days))
        entry.refresh_from_db()
        return entry

    def test_fresh_entry_is_not_stale(self):
        entry = self._make_entry(age_days=0.1)
        self.assertFalse(entry.is_stale)

    def test_entry_older_than_default_week_is_stale(self):
        entry = self._make_entry(age_days=8)
        self.assertTrue(entry.is_stale)

    def test_admin_can_extend_minimum_cache_duration(self):
        site_settings = SiteSettings.get_current()
        site_settings.external_data_cache_days = 30
        site_settings.save()

        entry = self._make_entry(age_days=10)

        self.assertFalse(entry.is_stale)

    @given(configured_days=st.integers(min_value=1, max_value=365), age_days=st.floats(min_value=0, max_value=400, allow_nan=False))
    @_hyp
    def test_staleness_matches_configured_threshold(self, configured_days: int, age_days: float):
        site_settings = SiteSettings.get_current()
        site_settings.external_data_cache_days = configured_days
        site_settings.save()

        entry = self._make_entry(age_days=age_days)

        self.assertEqual(entry.is_stale, age_days > configured_days)


class LocationCacheSharingTests(TestCase):
    """A single LocationCache row is shared across every Pin/Wiki at the same Location."""

    def test_get_fresh_returns_entry_regardless_of_which_pin_wrote_it(self):
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile

        location = baker.make(Location, latitude=40.0, longitude=-74.0)
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        # A Pin is unique per (location, profile), so two pins sharing a Location
        # need two different profiles -- e.g. two different users pinning the same place.
        user_a = baker.make("auth.User")
        user_b = baker.make("auth.User")
        pin_a = baker.make(Pin, location=location, profile=Profile.objects.get(user=user_a))
        pin_b = baker.make(Pin, location=location, profile=Profile.objects.get(user=user_b))

        LocationCache.set(pin_a.location, "nps", {"found": True}, query_key="Shared Place")

        cached_for_b = LocationCache.get_fresh(pin_b.location, "nps")

        self.assertIsNotNone(cached_for_b)
        self.assertEqual(cached_for_b.data, {"found": True})

    def test_missing_entry_returns_none(self):
        location = baker.make(Location, latitude=40.0, longitude=-74.0)
        self.assertIsNone(LocationCache.get_fresh(location, "nps"))
