"""Tests for the unfiled-photo ingestion path: find_matching_pin and _suggest_for_unfiled_photo.

These cover Memories-page uploads (no pin attached): the photo's GPS is matched
against the uploader's own pins to raise a self-directed VisitSuggestion. Pin.point
is set explicitly on fixtures because it is never auto-synced from latitude/longitude
in Python (see test_memories_visits).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.services.memories.photos import find_matching_pin
from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit

_PIN_LAT = 40.0
_PIN_LNG = -74.0


class FindMatchingPinTests(TestCase):
    """find_matching_pin() returns the nearest root pin within the match radius, else None."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=str(_PIN_LAT), longitude=str(_PIN_LNG))
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
            latitude=None,
            longitude=None,
            point=Point(_PIN_LNG, _PIN_LAT, srid=4326),
        )

    def test_matches_pin_within_radius(self):
        # ~40m north-east of the pin.
        pin = find_matching_pin(self.profile, _PIN_LAT + 0.0003, _PIN_LNG + 0.0003)
        self.assertEqual(pin, self.pin)

    def test_no_match_outside_radius(self):
        # >100km away.
        self.assertIsNone(find_matching_pin(self.profile, _PIN_LAT + 1.0, _PIN_LNG + 1.0))

    def test_no_match_for_other_profiles_pin(self):
        other = baker.make("auth.User").profile
        self.assertIsNone(find_matching_pin(other, _PIN_LAT, _PIN_LNG))

    def test_returns_nearest_of_several(self):
        near = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            latitude=Decimal(str(_PIN_LAT + 0.0001)),
            longitude=Decimal(str(_PIN_LNG + 0.0001)),
            point=Point(_PIN_LNG + 0.0001, _PIN_LAT + 0.0001, srid=4326),
        )
        match = find_matching_pin(self.profile, _PIN_LAT + 0.00012, _PIN_LNG + 0.00012)
        self.assertEqual(match, near)


class SuggestForUnfiledPhotoTests(TestCase):
    """maybe_suggest_photo_visit() raises a suggestion for an unfiled photo near a pin."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=str(_PIN_LAT), longitude=str(_PIN_LNG))
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
            latitude=None,
            longitude=None,
            point=Point(_PIN_LNG, _PIN_LAT, srid=4326),
        )

    def _unfiled_photo(self, lat, lng, taken_at):
        return baker.make(
            "dashboard.Image",
            pin=None,
            wiki=None,
            profile=self.profile,
            latitude=None if lat is None else Decimal(str(lat)),
            longitude=None if lng is None else Decimal(str(lng)),
            taken_at=taken_at,
        )

    def test_creates_suggestion_when_near_a_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._unfiled_photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)

        suggestion = maybe_suggest_photo_visit(photo)

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.origin_image_id, photo.pk)
        self.assertEqual(suggestion.location_id, self.location.pk)
        self.assertTrue(suggestion.is_from_photo)

    def test_no_suggestion_when_far_from_all_pins(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._unfiled_photo(_PIN_LAT + 1.0, _PIN_LNG + 1.0, taken_at)
        self.assertIsNone(maybe_suggest_photo_visit(photo))

    def test_no_suggestion_without_coordinates(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = self._unfiled_photo(None, None, taken_at)
        self.assertIsNone(maybe_suggest_photo_visit(photo))

    def test_batch_same_day_yields_one_suggestion(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        p1 = self._unfiled_photo(_PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)
        p2 = self._unfiled_photo(_PIN_LAT + 0.0004, _PIN_LNG + 0.0004, taken_at)

        maybe_suggest_photo_visit(p1)
        maybe_suggest_photo_visit(p2)

        self.assertEqual(VisitSuggestion.objects.filter(suggested_to=self.profile).count(), 1)

    def test_photo_attached_to_wiki_is_not_treated_as_unfiled(self):
        # A wiki-gallery upload (wiki set, no pin) must not raise a
        # visit suggestion - it isn't an unfiled Memories upload.
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        wiki = baker.make("dashboard.Wiki", location=self.location)
        photo = baker.make(
            "dashboard.Image",
            pin=None,
            wiki=wiki,
            profile=self.profile,
            latitude=Decimal(str(_PIN_LAT + 0.0003)),
            longitude=Decimal(str(_PIN_LNG + 0.0003)),
            taken_at=taken_at,
        )
        self.assertIsNone(maybe_suggest_photo_visit(photo))
