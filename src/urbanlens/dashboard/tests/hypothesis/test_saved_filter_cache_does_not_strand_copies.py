"""A fingerprint-versioned key is self-invalidating, not self-cleaning.

``saved_filter_pins:<profile>:<filter>:<filter_updated>:<fingerprint>`` holds
every uuid a saved filter matches. The fingerprint changes on every pin create,
edit and delete, which is what makes a stale entry unreadable - but the stale
entry is still *there*, holding its bytes for the full day of the TTL, in the
512MB Valkey that also holds sessions, the Channels layer and the Celery broker
under `volatile-lru`. So an ordinary afternoon of editing pins leaves one dead
copy of the account's matching-uuid list per edit, per saved filter, and the
eviction that makes room for them takes other people's sessions.

Three properties, and the second and third are a defect class this effort has
already fixed twice elsewhere:

- the superseded copy goes when its replacement is written;
- the list is not stored at all when it is too big to be worth storing;
- a cache that is full or unreachable does not turn a map load into a 500,
  because `get_or_compute_matching_uuids` answers from the database anyway.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.search import saved_filter_cache
from urbanlens.dashboard.services.search.saved_filter_cache import get_or_compute_matching_uuids, pins_fingerprint


class SavedFilterCacheTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.saved_filter = baker.make(SavedFilter, profile=self.profile, criteria={})
        for _ in range(3):
            baker.make(Pin, profile=self.profile, location=baker.make(Location))
        cache.clear()

    def _key(self, fingerprint: str) -> str:
        return saved_filter_cache._CACHE_KEY_TEMPLATE.format(
            profile_id=self.profile.pk,
            filter_uuid=self.saved_filter.uuid,
            filter_updated=self.saved_filter.updated.isoformat(),
            fingerprint=fingerprint,
        )

    def test_the_superseded_copy_is_deleted_when_the_pins_change(self) -> None:
        first_fingerprint = pins_fingerprint(self.profile)
        get_or_compute_matching_uuids(self.profile, self.saved_filter)
        self.assertIsNotNone(
            cache.get(self._key(first_fingerprint)), "nothing was cached, so there is nothing to strand"
        )

        baker.make(Pin, profile=self.profile, location=baker.make(Location))
        second_fingerprint = pins_fingerprint(self.profile)
        self.assertNotEqual(
            first_fingerprint, second_fingerprint, "the fingerprint did not move, so this proves nothing"
        )
        get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertIsNotNone(cache.get(self._key(second_fingerprint)), "the new entry was not written")
        self.assertIsNone(cache.get(self._key(first_fingerprint)), "the superseded copy is still holding its bytes")

    def test_a_warm_entry_is_still_served_from_the_cache(self) -> None:
        """The negative half: deleting the old copy must not delete the live one.

        The fingerprint is passed in so the aggregate that computes it does not
        run here - otherwise a warm call still touches ``Pin`` and the query
        assertion means nothing.
        """
        fingerprint = pins_fingerprint(self.profile)
        expected = get_or_compute_matching_uuids(self.profile, self.saved_filter, fingerprint=fingerprint)

        with mock.patch.object(Pin, "objects", wraps=Pin.objects) as pin_objects:
            again = get_or_compute_matching_uuids(self.profile, self.saved_filter, fingerprint=fingerprint)

        self.assertEqual(again, expected)
        pin_objects.filter.assert_not_called()
        self.assertIsNotNone(cache.get(self._key(fingerprint)))

    def test_a_list_over_the_ceiling_is_not_stored(self) -> None:
        with override_settings(SAVED_FILTER_MAX_CACHED_UUIDS=2):
            uuids = get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertEqual(len(uuids), 3, "the answer must be complete whether or not it was cached")
        self.assertIsNone(cache.get(self._key(pins_fingerprint(self.profile))))

    def test_a_list_under_the_ceiling_is_stored(self) -> None:
        """The negative half of the ceiling: without this, a broken set() passes above."""
        with override_settings(SAVED_FILTER_MAX_CACHED_UUIDS=100):
            get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertIsNotNone(cache.get(self._key(pins_fingerprint(self.profile))))

    def test_the_ceiling_setting_exists_in_the_real_settings_module(self) -> None:
        """An override that invents a setting proves nothing about production."""
        from django.conf import settings as django_settings

        self.assertIsInstance(django_settings.SAVED_FILTER_MAX_CACHED_UUIDS, int)

    def test_an_unwritable_cache_does_not_break_the_answer(self) -> None:
        expected = {str(pin.uuid) for pin in Pin.objects.filter(profile=self.profile)}

        with mock.patch.object(cache, "set", side_effect=ConnectionError("valkey is full")):
            uuids = get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertEqual(set(uuids), expected)

    def test_an_unreadable_cache_does_not_break_the_answer(self) -> None:
        expected = {str(pin.uuid) for pin in Pin.objects.filter(profile=self.profile)}

        with mock.patch.object(cache, "get", side_effect=ConnectionError("valkey is down")):
            uuids = get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertEqual(set(uuids), expected)

    def test_an_undeletable_cache_does_not_break_the_answer(self) -> None:
        get_or_compute_matching_uuids(self.profile, self.saved_filter)
        baker.make(Pin, profile=self.profile, location=baker.make(Location))
        expected = {str(pin.uuid) for pin in Pin.objects.filter(profile=self.profile)}

        with mock.patch.object(cache, "delete", side_effect=ConnectionError("valkey is down")):
            uuids = get_or_compute_matching_uuids(self.profile, self.saved_filter)

        self.assertEqual(set(uuids), expected)
