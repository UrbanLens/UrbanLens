"""Tests for the photo-organize services: classify_photo, create_pin_and_log_visit, log_visit_on_pin."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.photos import classify_photo, create_pin_and_log_visit, log_visit_on_pin

_LAT = 41.5
_LNG = -73.5


class ClassifyPhotoTests(TestCase):
    """classify_photo() reports the organize state a photo is in."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def _photo(self, *, lat=None, lng=None, visit=None, dismissed=False):
        return baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            location=None,
            visit=visit,
            organize_dismissed=dismissed,
            latitude=lat if lat is None else Decimal(str(lat)),
            longitude=lng if lng is None else Decimal(str(lng)),
        )

    def test_needs_location_without_coords(self):
        self.assertEqual(classify_photo(self._photo()), "needs_location")

    def test_needs_pin_with_coords(self):
        self.assertEqual(classify_photo(self._photo(lat=_LAT, lng=_LNG)), "needs_pin")

    def test_filed_when_attached_to_visit(self):
        pin = baker.make("dashboard.Pin", profile=self.profile, latitude=Decimal(str(_LAT)), longitude=Decimal(str(_LNG)), point=Point(_LNG, _LAT, srid=4326))
        visit = baker.make("dashboard.PinVisit", pin=pin, visited_at=timezone.now())
        self.assertEqual(classify_photo(self._photo(lat=_LAT, lng=_LNG, visit=visit)), "filed")

    def test_filed_when_dismissed(self):
        self.assertEqual(classify_photo(self._photo(lat=_LAT, lng=_LNG, dismissed=True)), "filed")

    def test_suggested_when_pending_suggestion_exists(self):
        photo = self._photo(lat=_LAT, lng=_LNG)
        baker.make(
            "dashboard.VisitSuggestion",
            suggested_to=self.profile,
            origin_image=photo,
            origin_visit=None,
            trip_activity=None,
            safety_checkin=None,
            latitude=Decimal(str(_LAT)),
            longitude=Decimal(str(_LNG)),
            visited_at=timezone.now(),
        )
        self.assertEqual(classify_photo(photo), "suggested")


class CreatePinAndLogVisitTests(TestCase):
    """create_pin_and_log_visit() makes a pin at the photo's coords and logs a visit."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.taken_at = timezone.make_aware(datetime.datetime(2024, 5, 4, 9, 0, 0))
        self.photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            location=None,
            latitude=Decimal(str(_LAT)),
            longitude=Decimal(str(_LNG)),
            taken_at=self.taken_at,
        )

    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_creates_pin_visit_and_attaches_photo(self, mock_enqueue):
        pin, visit = create_pin_and_log_visit(self.profile, self.photo)

        self.assertEqual(pin.profile_id, self.profile.pk)
        self.assertEqual(Decimal(str(pin.latitude)), Decimal(str(_LAT)))
        self.assertEqual(visit.source, VisitSource.PHOTO)
        self.assertEqual(visit.visited_at, self.taken_at)

        self.photo.refresh_from_db()
        self.assertEqual(self.photo.visit_id, visit.pk)
        self.assertEqual(self.photo.pin_id, pin.pk)
        # A background task is enqueued to resolve the pin's Location.
        self.assertTrue(mock_enqueue.called)

    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_raises_without_coordinates(self, _mock_enqueue):
        photo = baker.make("dashboard.Image", profile=self.profile, pin=None, location=None, latitude=None, longitude=None)
        with self.assertRaises(ValueError):
            create_pin_and_log_visit(self.profile, photo)


class LogVisitOnPinTests(TestCase):
    """log_visit_on_pin() logs a photo-sourced visit and back-fills missing coords."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            latitude=Decimal(str(_LAT)),
            longitude=Decimal(str(_LNG)),
            point=Point(_LNG, _LAT, srid=4326),
        )

    def test_logs_visit_and_backfills_coords(self):
        photo = baker.make("dashboard.Image", profile=self.profile, pin=None, location=None, latitude=None, longitude=None)

        visit = log_visit_on_pin(self.profile, photo, self.pin)

        self.assertEqual(visit.pin_id, self.pin.pk)
        self.assertEqual(visit.source, VisitSource.PHOTO)
        photo.refresh_from_db()
        self.assertEqual(photo.visit_id, visit.pk)
        self.assertEqual(photo.pin_id, self.pin.pk)
        self.assertEqual(Decimal(str(photo.latitude)), Decimal(str(_LAT)))

    def test_keeps_existing_photo_coords(self):
        photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            location=None,
            latitude=Decimal("10.0"),
            longitude=Decimal("20.0"),
        )

        log_visit_on_pin(self.profile, photo, self.pin)

        photo.refresh_from_db()
        self.assertEqual(Decimal(str(photo.latitude)), Decimal("10.0"))
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 1)
