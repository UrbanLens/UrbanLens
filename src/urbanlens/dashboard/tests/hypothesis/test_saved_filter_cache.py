"""Tests for the saved-filter matching-pins cache.

Covers the stale-cache regression: editing a saved filter's criteria (with no
pin itself edited in between) must invalidate the cached matching-uuid list,
since the cache key previously only embedded a fingerprint of the profile's
pins, never anything derived from the filter's own criteria/updated timestamp
- so editing a filter served the OLD (sometimes empty) cached result forever,
while the Lists page's smart-list matching (which never caches this at all)
correctly reflected the new criteria immediately. This is exactly why a
saved filter could show 400+ matches as a smart list but 0 via the map
toolbar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.saved_filter_cache import get_or_compute_matching_uuids

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class SavedFilterCacheInvalidationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile: Profile = baker.make(User).profile
        self.tagged_pin = baker.make(
            Pin,
            profile=self.profile,
            name="Tagged Pin",
            location=baker.make(Location, latitude=40.0, longitude=-74.0),
        )
        self.other_pin = baker.make(
            Pin,
            profile=self.profile,
            name="Other Pin",
            location=baker.make(Location, latitude=41.0, longitude=-75.0),
        )

    def test_editing_criteria_invalidates_the_cache(self) -> None:
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="My Filter", criteria={"name": "Other"})
        first = get_or_compute_matching_uuids(self.profile, saved_filter)
        self.assertEqual(first, [str(self.other_pin.uuid)])

        # Edit the filter's criteria to match a different pin - no Pin itself
        # is touched, so the old bug's cache key (pins-fingerprint only)
        # would stay identical and keep serving the stale "Other" result.
        saved_filter.criteria = {"name": "Tagged"}
        saved_filter.save(update_fields=["criteria", "updated"])

        second = get_or_compute_matching_uuids(self.profile, saved_filter)
        self.assertEqual(second, [str(self.tagged_pin.uuid)])

    def test_editing_criteria_to_match_nothing_then_back_is_not_stuck(self) -> None:
        """Regression guard for the exact reported symptom: a filter that legitimately
        matched 0 pins at some point (e.g. right after creation) must not keep
        returning that empty result forever once its criteria is fixed."""
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="My Filter", criteria={"name": "Nothing Matches This"})
        empty_result = get_or_compute_matching_uuids(self.profile, saved_filter)
        self.assertEqual(empty_result, [])

        saved_filter.criteria = {}
        saved_filter.save(update_fields=["criteria", "updated"])

        full_result = get_or_compute_matching_uuids(self.profile, saved_filter)
        self.assertEqual(set(full_result), {str(self.tagged_pin.uuid), str(self.other_pin.uuid)})

    def test_unchanged_filter_is_served_from_cache(self) -> None:
        """Sanity check the cache still works at all - not just always recomputing."""
        saved_filter = SavedFilter.objects.create(profile=self.profile, name="My Filter", criteria={"name": "Tagged"})
        get_or_compute_matching_uuids(self.profile, saved_filter)

        # Delete the pin the filter matched entirely from the DB without saving
        # the filter or the pin - if this still returns the (now-stale) cached
        # uuid, the cache was actually used rather than recomputed.
        deleted_uuid = str(self.tagged_pin.uuid)
        Pin.objects.filter(pk=self.tagged_pin.pk).delete()
        # Deleting a pin doesn't touch any surviving pin's `updated`, so the
        # pins-fingerprint half of the key is unchanged; only an explicit
        # re-save of a surviving pin (or the filter) should bust this.
        cached_again = get_or_compute_matching_uuids(self.profile, saved_filter)
        self.assertEqual(cached_again, [deleted_uuid])
