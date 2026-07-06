"""Tests for services.memories.visits.maybe_create_photo_visit().

All tests require the database - Pin.point (the field distance queries run
against) is never auto-synced from latitude/longitude in Python, so it must
be set explicitly on the baked fixture, matching how Pin creation paths
(e.g. Pin.objects.get_nearby_or_create) always pass `point=` explicitly.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.visits import maybe_create_photo_visit

_PIN_LAT = 40.0
_PIN_LNG = -74.0


def _photo(pin, lat: float, lng: float, taken_at: datetime.datetime | None) -> Image:
    return Image(pin=pin, latitude=Decimal(str(lat)), longitude=Decimal(str(lng)), taken_at=taken_at)


class MaybeCreatePhotoVisitTests(TestCase):
    """maybe_create_photo_visit() creates a PinVisit(source=PHOTO) for nearby, timestamped photos."""

    def setUp(self):
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

    def test_creates_visit_when_photo_is_near_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = _photo(self.pin, _PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)  # ~40m away

        visit = maybe_create_photo_visit(photo)

        self.assertIsNotNone(visit)
        self.assertEqual(visit.source, VisitSource.PHOTO)
        self.assertEqual(visit.pin_id, self.pin.pk)
        self.assertEqual(visit.visited_at, taken_at)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.PHOTO).count(), 1)

    def test_does_not_create_visit_when_photo_is_far_from_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = _photo(self.pin, _PIN_LAT + 1.0, _PIN_LNG + 1.0, taken_at)  # >100km away

        visit = maybe_create_photo_visit(photo)

        self.assertIsNone(visit)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 0)

    def test_duplicate_photo_visit_is_not_created_twice(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = _photo(self.pin, _PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)

        maybe_create_photo_visit(photo)
        maybe_create_photo_visit(photo)

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.PHOTO).count(), 1)

    def test_no_visit_without_taken_at(self):
        photo = _photo(self.pin, _PIN_LAT, _PIN_LNG, None)
        self.assertIsNone(maybe_create_photo_visit(photo))

    def test_no_visit_without_coordinates(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = Image(pin=self.pin, latitude=None, longitude=None, taken_at=taken_at)
        self.assertIsNone(maybe_create_photo_visit(photo))

    def test_no_visit_without_pin(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = Image(pin=None, latitude=Decimal(str(_PIN_LAT)), longitude=Decimal(str(_PIN_LNG)), taken_at=taken_at)
        self.assertIsNone(maybe_create_photo_visit(photo))

    def test_updates_pin_last_visited(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        photo = _photo(self.pin, _PIN_LAT + 0.0003, _PIN_LNG + 0.0003, taken_at)

        maybe_create_photo_visit(photo)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, taken_at)
