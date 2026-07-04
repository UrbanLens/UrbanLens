"""Tests for the pin_invalidate_map_center post_save signal.

Invariants verified:
  - Creating a new Pin clears the profile's cached centroid (lat/lng → None).
  - Saving an *existing* Pin does NOT clear the cache.
  - A Pin with no profile_id does not crash and does not affect any profile.
"""
from __future__ import annotations

import decimal

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_db_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_CACHED_LAT = decimal.Decimal("42.650000")
_CACHED_LNG = decimal.Decimal("-73.750000")


def _set_cached_centroid(profile: Profile) -> None:
    Profile.objects.filter(pk=profile.pk).update(
        map_center_latitude=_CACHED_LAT,
        map_center_longitude=_CACHED_LNG,
    )


class InvalidateMapCenterOnCreateTests(TestCase):
    """Creating a new pin must clear the profile's cached centroid."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        _set_cached_centroid(self.profile)

    def test_new_pin_clears_cached_latitude(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_latitude)

    def test_new_pin_clears_cached_longitude(self) -> None:
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_longitude)

    def test_second_pin_also_clears_cache(self) -> None:
        # First pin clears it; then the cache is recomputed externally...
        baker.make(Pin, profile=self.profile)
        _set_cached_centroid(self.profile)
        # ...and a second pin must clear it again.
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_latitude)

    @given(n=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_creating_n_pins_always_clears_centroid_cache(self, n: int) -> None:
        _set_cached_centroid(self.profile)
        for _ in range(n):
            baker.make(Pin, profile=self.profile)
            _set_cached_centroid(self.profile)  # simulate recompute between pins
        # After one final pin, the cache is cleared.
        baker.make(Pin, profile=self.profile)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_latitude)
        self.assertIsNone(self.profile.map_center_longitude)


class InvalidateMapCenterOnUpdateTests(TestCase):
    """Updating an existing pin must NOT clear the cached centroid."""

    profile: Profile

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile)
        _set_cached_centroid(self.profile)

    def test_saving_existing_pin_does_not_clear_cache(self) -> None:
        self.pin.name = "Updated name"
        self.pin.save()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)
        self.assertIsNotNone(self.profile.map_center_longitude)

    def test_multiple_updates_do_not_clear_cache(self) -> None:
        for i in range(3):
            self.pin.name = f"Update {i}"
            self.pin.save()
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)


class InvalidateMapCenterNoProfileTests(TestCase):
    """A pin saved without a profile_id must not raise and must not touch any profile."""

    def test_pin_without_profile_does_not_raise(self) -> None:
        # baker.make sets profile; we test the signal guard by calling it directly.
        from urbanlens.dashboard.models.pin.signals import invalidate_profile_map_center
        other_profile: Profile = baker.make(User).profile
        _set_cached_centroid(other_profile)

        # Construct an unsaved Pin with no profile_id to test the guard.
        fake_pin = Pin()
        fake_pin.profile_id = None

        # Must not raise.
        invalidate_profile_map_center(sender=Pin, instance=fake_pin, created=True)

        # No side-effects on other profiles.
        other_profile.refresh_from_db()
        self.assertIsNotNone(other_profile.map_center_latitude)
