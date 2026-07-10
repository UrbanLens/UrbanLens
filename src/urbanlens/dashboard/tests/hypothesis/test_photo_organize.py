"""Tests for the photo-organize services: classify_photo, create_pin_and_log_visit, log_visit_on_pin."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
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

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_files_same_day_sibling_photos_directly(self, _mock_enqueue, _mock_resolve_name):
        # A second photo from the same drop, close enough to match the pin the
        # first photo is about to create, taken the same day. create_visit_suggestion
        # would silently no-op here (a visit already exists for that day/place and
        # no new participants would be added), so it must be filed directly instead
        # of left stuck offering "create a pin" for a place that already has one.
        sibling = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            latitude=Decimal(str(_LAT + 0.0003)),
            longitude=Decimal(str(_LNG + 0.0003)),
            taken_at=self.taken_at,
        )

        pin, visit = create_pin_and_log_visit(self.profile, self.photo)

        sibling.refresh_from_db()
        self.assertEqual(sibling.pin_id, pin.pk)
        self.assertIsNotNone(sibling.visit_id)
        self.assertNotEqual(sibling.visit_id, visit.pk)
        self.assertEqual(PinVisit.objects.filter(pin=pin).count(), 2)
        # The photo that was directly turned into the pin isn't re-suggested.
        self.assertFalse(VisitSuggestion.objects.filter(origin_image=self.photo).exists())

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_resuggests_nearby_photos_from_a_different_day(self, _mock_enqueue, _mock_resolve_name):
        # A photo at the same spot but from an earlier trip - not obviously the
        # same visit, so it should get a normal confirmable suggestion instead
        # of being silently filed.
        older_taken_at = self.taken_at - datetime.timedelta(days=30)
        older_photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            latitude=Decimal(str(_LAT + 0.0003)),
            longitude=Decimal(str(_LNG + 0.0003)),
            taken_at=older_taken_at,
        )

        pin, _visit = create_pin_and_log_visit(self.profile, self.photo)

        suggestion = VisitSuggestion.objects.filter(origin_image=older_photo).first()
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.location_id, pin.location_id)
        older_photo.refresh_from_db()
        self.assertIsNone(older_photo.pin_id)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_does_not_resuggest_photos_already_filed_or_dismissed(self, _mock_enqueue, _mock_resolve_name):
        filed = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=baker.make("dashboard.Pin", profile=self.profile),
            wiki=None,
            latitude=Decimal(str(_LAT + 0.0002)),
            longitude=Decimal(str(_LNG + 0.0002)),
            taken_at=self.taken_at,
        )
        dismissed = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            organize_dismissed=True,
            latitude=Decimal(str(_LAT + 0.0002)),
            longitude=Decimal(str(_LNG + 0.0002)),
            taken_at=self.taken_at,
        )

        create_pin_and_log_visit(self.profile, self.photo)

        self.assertFalse(VisitSuggestion.objects.filter(origin_image=filed).exists())
        self.assertFalse(VisitSuggestion.objects.filter(origin_image=dismissed).exists())

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_reuses_existing_pin_instead_of_colliding(self, _mock_enqueue, _mock_resolve_name):
        # Simulates the staging bug: a second, unrelated photo resolves to the
        # same Location as one that already has a pin (e.g. a stale "create a
        # pin" card the resuggestion path didn't reach, or two photos just
        # happening to land on the same spot) - it must reuse that pin rather
        # than violate db_pin_unique_location_per_profile. Created only after
        # the first call completes so it isn't itself swept up by that call's
        # own resuggestion pass (covered separately above).
        first_pin, first_visit = create_pin_and_log_visit(self.profile, self.photo)

        second_photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            # Close enough that Location.objects.get_for_point() resolves to the
            # same Location (50 m proximity fallback) as the first photo.
            latitude=Decimal(str(_LAT + 0.0001)),
            longitude=Decimal(str(_LNG + 0.0001)),
            taken_at=self.taken_at,
        )
        second_pin, second_visit = create_pin_and_log_visit(self.profile, second_photo)

        self.assertEqual(second_pin.pk, first_pin.pk)
        self.assertEqual(Pin.objects.filter(profile=self.profile, location_id=first_pin.location_id).count(), 1)
        self.assertNotEqual(second_visit.pk, first_visit.pk)
        self.assertEqual(PinVisit.objects.filter(pin=first_pin).count(), 2)

    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_reused_pin_keeps_its_existing_name(self, _mock_enqueue, _mock_resolve_name):
        create_pin_and_log_visit(self.profile, self.photo, name="Old Water Tower")

        second_photo = baker.make(
            "dashboard.Image",
            profile=self.profile,
            pin=None,
            wiki=None,
            latitude=Decimal(str(_LAT + 0.0001)),
            longitude=Decimal(str(_LNG + 0.0001)),
            taken_at=self.taken_at,
        )
        second_pin, _visit = create_pin_and_log_visit(self.profile, second_photo, name="Different Name")

        self.assertEqual(second_pin.name, "Old Water Tower")


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
            image=SimpleUploadedFile("photo.jpg", b"photo-bytes", content_type="image/jpeg"),
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
        self.assertIn("refreshQueue", response["HX-Trigger"])
        self.photo.refresh_from_db()
        self.assertIsNotNone(self.photo.pin_id)
        self.assertEqual(self.photo.pin.name, "Ridge Overlook")
        self.assertEqual(Decimal(str(self.photo.pin.location.latitude)), Decimal("42.25"))

    def test_create_pin_post_is_a_no_op_when_already_filed(self):
        from django.urls import reverse

        pin = baker.make("dashboard.Pin", profile=self.profile)
        self.photo.pin = pin
        self.photo.visit = baker.make("dashboard.PinVisit", pin=pin)
        self.photo.save(update_fields=["pin", "visit"])

        response = self.client.post(
            reverse("memories.photos.action", args=[self.photo.pk, "create-pin"]),
            {"latitude": "42.250000", "longitude": "-71.750000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("already been filed", response["HX-Trigger"])
        self.assertEqual(PinVisit.objects.filter(pin=pin).count(), 1)

    @mock.patch("urbanlens.dashboard.controllers.photos.create_pin_and_log_visit", side_effect=RuntimeError("boom"))
    def test_create_pin_post_surfaces_unexpected_errors_as_a_toast(self, _mock_create):
        from django.urls import reverse

        response = self.client.post(
            reverse("memories.photos.action", args=[self.photo.pk, "create-pin"]),
            {"latitude": "42.250000", "longitude": "-71.750000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Something went wrong", response["HX-Trigger"])
        self.photo.refresh_from_db()
        self.assertIsNone(self.photo.pin_id)


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
