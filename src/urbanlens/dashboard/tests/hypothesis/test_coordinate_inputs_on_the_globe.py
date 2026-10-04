"""P282: a coordinate a form posts is a finite number on the globe before anything is placed at it.

Filing a photo as a new pin and placing an import failure by hand parsed the posted latitude and longitude with a bare
``float()``, which reads ``inf``, ``nan`` and ``1e999``, and stored whatever came out, a latitude past the poles too.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_import_failures.model import (
    PinImportFailure,
    PinImportFailureReason,
    PinImportFailureStatus,
)

_OFF_THE_GLOBE = [("95", "-74"), ("40", "-200"), ("inf", "-74"), ("40", "-inf"), ("nan", "-74"), ("1e999", "-74")]


class _PostsCoordinates(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)

    def assert_nothing_placed(self) -> None:
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())
        self.assertFalse(Location.objects.filter(latitude__gt=90).exists())
        self.assertFalse(Location.objects.filter(latitude__lt=-90).exists())


class FilingAPhotoAsANewPinTests(_PostsCoordinates):
    def setUp(self) -> None:
        super().setUp()
        self.photo = baker.make(
            Image,
            profile=self.profile,
            pin=None,
            wiki=None,
            latitude=Decimal("41.5"),
            longitude=Decimal("-72.5"),
            image=SimpleUploadedFile("photo.jpg", b"photo-bytes", content_type="image/jpeg"),
        )

    def test_a_place_off_the_globe_files_nothing(self) -> None:
        url = reverse("vault.photos.action", args=[self.photo.pk, "create-pin"])
        for latitude, longitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.post(url, {"latitude": latitude, "longitude": longitude, "name": "Ridge"})

                self.assertEqual(response.status_code, 200)
                self.assert_nothing_placed()
                self.photo.refresh_from_db()
                self.assertIsNone(self.photo.pin_id)


class PlacingAnImportFailureByHandTests(_PostsCoordinates):
    def setUp(self) -> None:
        super().setUp()
        self.failure = PinImportFailure.objects.create(
            profile=self.profile, cid=12345, name="Cresson", reason=PinImportFailureReason.NO_LOCATION_FOUND
        )

    def test_a_place_off_the_globe_is_refused(self) -> None:
        url = reverse("memories.locations.import_failures.resolve", args=[self.failure.pk])
        for latitude, longitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude, longitude=longitude):
                response = self.client.post(url, {"latitude": latitude, "longitude": longitude})

                self.assertIn("Enter a valid latitude and longitude.", response.get("HX-Trigger", ""))
                self.assert_nothing_placed()
                self.failure.refresh_from_db()
                self.assertEqual(self.failure.status, PinImportFailureStatus.PENDING)

    def test_a_place_on_the_globe_is_placed(self) -> None:
        url = reverse("memories.locations.import_failures.resolve", args=[self.failure.pk])

        self.client.post(url, {"latitude": "41.5", "longitude": "-72.5"})

        self.failure.refresh_from_db()
        self.assertEqual(self.failure.status, PinImportFailureStatus.RESOLVED)
