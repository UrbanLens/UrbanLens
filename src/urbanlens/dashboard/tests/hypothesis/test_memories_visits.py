"""Tests for services.memories.visits.maybe_suggest_photo_visit().

All tests require the database. A Pin's coordinates (the field distance
queries run against) live on its linked Location, whose PostGIS point is
auto-synced from latitude/longitude on save.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit

_PIN_LAT = 40.0
_PIN_LNG = -74.0


class MaybeSuggestPhotoVisitTests(TestCase):
    """maybe_suggest_photo_visit() raises a self-directed VisitSuggestion for nearby, timestamped photos."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=str(_PIN_LAT), longitude=str(_PIN_LNG))
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
        )

    def _photo(self, lat: float | None, lng: float | None, taken_at, *, pin="__default__"):
        return baker.make(
            "dashboard.Image",
            pin=self.pin if pin == "__default__" else pin,
            profile=self.profile,
            latitude=None if lat is None else Decimal(str(lat)),
            longitude=None if lng is None else Decimal(str(lng)),
            taken_at=taken_at,
        )

    def test_creates_suggestion_when_photo_is_near_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)  # ~40m away

        suggestion = maybe_suggest_photo_visit(photo)

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.suggested_to_id, self.profile.pk)
        self.assertEqual(suggestion.origin_image_id, photo.pk)
        self.assertTrue(suggestion.is_from_photo)
        self.assertEqual(suggestion.status, VisitSuggestionStatus.PENDING)
        self.assertEqual(suggestion.visited_at, taken_at)
        # It must not silently create a PinVisit.
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 0)

    def test_no_suggestion_when_photo_is_far_from_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._photo(_PIN_LAT + 1.0, _PIN_LNG + 1.0, taken_at)  # >100km away

        self.assertIsNone(maybe_suggest_photo_visit(photo))
        self.assertFalse(VisitSuggestion.objects.exists())

    def test_batch_upload_same_day_yields_one_suggestion(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        p1 = self._photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)
        p2 = self._photo(_PIN_LAT + 0.0004, _PIN_LNG + 0.0004, taken_at)

        maybe_suggest_photo_visit(p1)
        maybe_suggest_photo_visit(p2)

        self.assertEqual(VisitSuggestion.objects.filter(suggested_to=self.profile).count(), 1)

    def test_no_suggestion_when_visit_already_exists_that_day(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        PinVisit.objects.create(pin=self.pin, visited_at=taken_at, source=VisitSource.MANUAL)
        photo = self._photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)

        self.assertIsNone(maybe_suggest_photo_visit(photo))
        self.assertFalse(VisitSuggestion.objects.exists())

    def test_no_suggestion_without_taken_at(self):
        photo = self._photo(_PIN_LAT, _PIN_LNG, None)
        self.assertIsNone(maybe_suggest_photo_visit(photo))

    def test_no_suggestion_without_coordinates(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._photo(None, None, taken_at)
        self.assertIsNone(maybe_suggest_photo_visit(photo))

    def test_unfiled_photo_near_pin_still_suggests(self):
        # An unfiled Memories upload (no pin attached) whose GPS lands near one of
        # the user's pins is matched to that pin via the unfiled-photo path.
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at, pin=None)

        suggestion = maybe_suggest_photo_visit(photo)

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.origin_image_id, photo.pk)
        self.assertEqual(suggestion.suggested_to_id, self.profile.pk)

    def test_no_suggestion_when_unfiled_photo_has_no_profile(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = baker.make(
            "dashboard.Image",
            pin=None,
            profile=None,
            latitude=Decimal(str(_PIN_LAT)),
            longitude=Decimal(str(_PIN_LNG)),
            taken_at=taken_at,
        )
        self.assertIsNone(maybe_suggest_photo_visit(photo))
