"""Tests for the photo-organize services: classify_photo, create_pin_and_log_visit, log_visit_on_pin."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

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
            wiki=None,
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
        location = baker.make("dashboard.Location", latitude=Decimal(str(_LAT)), longitude=Decimal(str(_LNG)))
        pin = baker.make("dashboard.Pin", profile=self.profile, location=location)
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
            wiki=None,
            latitude=Decimal(str(_LAT)),
            longitude=Decimal(str(_LNG)),
            taken_at=self.taken_at,
        )

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_creates_pin_visit_and_attaches_photo(self, _mock_enqueue, _mock_resolve_name):
        # No Location exists yet at these coordinates, so create_minimal_pin()
        # creates one via _create_location_with_canonical_name(), which resolves
        # a canonical place name from Google - mock that outbound call.
        pin, visit = create_pin_and_log_visit(self.profile, self.photo)

        self.assertEqual(pin.profile_id, self.profile.pk)
        self.assertEqual(Decimal(str(pin.location.latitude)), Decimal(str(_LAT)))
        self.assertEqual(visit.source, VisitSource.PHOTO)
        self.assertEqual(visit.visited_at, self.taken_at)

        self.photo.refresh_from_db()
        self.assertEqual(self.photo.visit_id, visit.pk)
        self.assertEqual(self.photo.pin_id, pin.pk)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_places_pin_at_override_coords_with_name(self, _mock_enqueue, _mock_resolve_name):
        # The confirmation dialog can move the marker and name the pin; the pin
        # lands at the override coords while the photo keeps its own capture coords.
        pin, _visit = create_pin_and_log_visit(self.profile, self.photo, latitude=42.25, longitude=-71.75, name="Old Water Tower")

        self.assertEqual(Decimal(str(pin.location.latitude)), Decimal("42.25"))
        self.assertEqual(Decimal(str(pin.location.longitude)), Decimal("-71.75"))
        self.assertEqual(pin.name, "Old Water Tower")
        self.assertTrue(pin.name_is_user_provided)
        # The photo's own coordinates are untouched (that's where it was taken).
        self.photo.refresh_from_db()
        self.assertEqual(Decimal(str(self.photo.latitude)), Decimal(str(_LAT)))

    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_raises_without_coordinates(self, _mock_enqueue):
        photo = baker.make("dashboard.Image", profile=self.profile, pin=None, wiki=None, latitude=None, longitude=None)
        with self.assertRaises(ValueError):
            create_pin_and_log_visit(self.profile, photo)


class PhotoPinConfirmViewTests(TestCase):
    """The confirm-pin dialog GET, and the create-pin POST honouring its placement."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            latitude=Decimal(str(_LAT)),
            longitude=Decimal(str(_LNG)),
            taken_at=timezone.make_aware(datetime.datetime(2024, 5, 4, 9, 0, 0)),
        )

    def test_confirm_dialog_renders_map_seeded_with_photo_coords(self):
        from django.urls import reverse

        response = self.client.get(reverse("memories.photos.pin_confirm", args=[self.photo.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "photo-pin-confirm-map")
        self.assertContains(response, 'data-lat="41.500000"')

    def test_confirm_dialog_404_for_photo_without_coords(self):
        from django.urls import reverse

        no_coords = baker.make("dashboard.Image", profile=self.profile, pin=None, wiki=None, latitude=None, longitude=None)
        response = self.client.get(reverse("memories.photos.pin_confirm", args=[no_coords.pk]))
        self.assertEqual(response.status_code, 404)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_create_pin_post_uses_confirmed_placement(self, _mock_enqueue, _mock_resolve_name):
        from django.urls import reverse

        response = self.client.post(
            reverse("memories.photos.action", args=[self.photo.pk, "create-pin"]),
            {"latitude": "42.250000", "longitude": "-71.750000", "name": "Ridge Overlook"},
        )

        self.assertEqual(response.status_code, 200)
        self.photo.refresh_from_db()
        self.assertIsNotNone(self.photo.pin_id)
        self.assertEqual(self.photo.pin.name, "Ridge Overlook")
        self.assertEqual(Decimal(str(self.photo.pin.location.latitude)), Decimal("42.25"))


class LogVisitOnPinTests(TestCase):
    """log_visit_on_pin() logs a photo-sourced visit and back-fills missing coords."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        location = baker.make("dashboard.Location", latitude=Decimal(str(_LAT)), longitude=Decimal(str(_LNG)))
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location)

    def test_logs_visit_and_backfills_coords(self):
        photo = baker.make("dashboard.Image", profile=self.profile, pin=None, wiki=None, latitude=None, longitude=None)

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
            wiki=None,
            latitude=Decimal("10.0"),
            longitude=Decimal("20.0"),
        )

        log_visit_on_pin(self.profile, photo, self.pin)

        photo.refresh_from_db()
        self.assertEqual(Decimal(str(photo.latitude)), Decimal("10.0"))
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 1)
