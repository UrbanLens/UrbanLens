"""
An answer that says a source is missing is shown, marked partial, and asked for again within the hour.

A partial answer cached for the whole window hides what the missing source would have added for days. Only an
answer with nothing in it was guarded before.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733000", longitude="-73.930000", google_place=None)
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=self.location)

    def row(self, source: str) -> LocationCache:
        return LocationCache.objects.get(location=self.location, source=source)

    def assert_partial_and_brief(self, source: str) -> None:
        row = self.row(source)
        self.assertTrue(row.data.get(UNANSWERED_SOURCES_KEY), row.data)
        self.assertIsNotNone(LocationCache.get_fresh(self.location, source))
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNone(LocationCache.get_fresh(self.location, source))


class LocationCacheTests(_Case):
    def test_a_payload_marked_partial_lapses_within_the_hour(self) -> None:
        LocationCache.set(self.location, "some_source", {"rows": [1], UNANSWERED_SOURCES_KEY: ["overture"]})

        self.assert_partial_and_brief("some_source")

    def test_an_unmarked_payload_keeps_the_window(self) -> None:
        LocationCache.set(self.location, "some_source", {"rows": [1]})

        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNotNone(LocationCache.get_fresh(self.location, "some_source"))
