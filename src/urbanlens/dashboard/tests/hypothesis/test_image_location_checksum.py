"""Tests for Image.location wiring, effective coordinates, and duplicate-upload checksums.

Covers:
- compute_checksum() - deterministic hashing and file-pointer rewind
- Image.effective_latitude/effective_longitude - own GPS preferred, location fallback
- _visit_dialog_context() - the photo picker excludes other visits' photos
- _sync_visit_photos() - sets location/checksum, reuses duplicates, never steals
  photos from other visits
"""

from __future__ import annotations

import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.visits import _sync_visit_photos, _visit_dialog_context
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.images import compute_checksum

_hyp = hyp_settings(max_examples=40, deadline=None)

_LAT = Decimal("41.500000")
_LNG = Decimal("-73.500000")


class ComputeChecksumTests(SimpleTestCase):
    """compute_checksum() hashes file content deterministically and rewinds."""

    @given(content=st.binary(min_size=1, max_size=4096))
    @_hyp
    def test_deterministic_and_rewound(self, content: bytes):
        fh = io.BytesIO(content)
        first = compute_checksum(fh)
        # The pointer is rewound, so hashing again yields the same digest.
        second = compute_checksum(fh)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(fh.tell(), 0)

    @given(a=st.binary(min_size=1, max_size=1024), b=st.binary(min_size=1, max_size=1024))
    @_hyp
    def test_digest_equality_matches_content_equality(self, a: bytes, b: bytes):
        self.assertEqual(compute_checksum(io.BytesIO(a)) == compute_checksum(io.BytesIO(b)), a == b)

    def test_hashes_from_middle_of_file(self):
        fh = io.BytesIO(b"hello world")
        fh.seek(5)
        self.assertEqual(compute_checksum(fh), compute_checksum(io.BytesIO(b"hello world")))


class EffectiveCoordinateTests(TestCase):
    """Image.effective_latitude/longitude prefer own GPS, then the linked Location."""

    def test_own_gps_wins(self):
        location = baker.prepare("dashboard.Location", latitude=Decimal("1"), longitude=Decimal("2"))
        img = Image(latitude=_LAT, longitude=_LNG, location=location)
        self.assertEqual(img.effective_latitude, _LAT)
        self.assertEqual(img.effective_longitude, _LNG)

    def test_falls_back_to_location(self):
        location = baker.make("dashboard.Location", latitude=_LAT, longitude=_LNG)
        img = Image(location=location)
        self.assertEqual(img.effective_latitude, _LAT)
        self.assertEqual(img.effective_longitude, _LNG)

    def test_none_when_nothing_known(self):
        img = Image()
        self.assertIsNone(img.effective_latitude)
        self.assertIsNone(img.effective_longitude)


class VisitDialogContextTests(TestCase):
    """_visit_dialog_context() only offers photos not attached to a different visit."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        location = baker.make("dashboard.Location", latitude=_LAT, longitude=_LNG)
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=location)
        self.visit = baker.make("dashboard.PinVisit", pin=self.pin, visited_at=timezone.now())
        self.other_visit = baker.make("dashboard.PinVisit", pin=self.pin, visited_at=timezone.now())
        self.unattached = baker.make("dashboard.Image", pin=self.pin, profile=self.profile, visit=None)
        self.on_this_visit = baker.make("dashboard.Image", pin=self.pin, profile=self.profile, visit=self.visit)
        self.on_other_visit = baker.make("dashboard.Image", pin=self.pin, profile=self.profile, visit=self.other_visit)

    def test_add_dialog_offers_only_unattached(self):
        images = _visit_dialog_context(self.pin)["pin_images"]
        self.assertEqual({img.pk for img in images}, {self.unattached.pk})

    def test_edit_dialog_keeps_own_photos(self):
        images = _visit_dialog_context(self.pin, visit=self.visit)["pin_images"]
        self.assertEqual({img.pk for img in images}, {self.unattached.pk, self.on_this_visit.pk})


class SyncVisitPhotosTests(TestCase):
    """_sync_visit_photos() creates deduplicated, location-linked images."""

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=_LAT, longitude=_LNG)
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.visit = baker.make("dashboard.PinVisit", pin=self.pin, visited_at=timezone.now())

    def _post(self, *, photos=None, existing_ids=None):
        data = {}
        if photos:
            data["photos"] = photos
        if existing_ids:
            data["existing_photo_ids"] = [str(pk) for pk in existing_ids]
        return self.factory.post("/", data)

    def test_new_upload_sets_location_and_checksum(self):
        request = self._post(photos=[SimpleUploadedFile("a.jpg", b"photo-bytes-a", content_type="image/jpeg")])
        uploaded = _sync_visit_photos(request, self.pin, self.visit)

        self.assertTrue(uploaded)
        img = Image.objects.get(pin=self.pin, visit=self.visit)
        self.assertEqual(img.location_id, self.location.pk)
        self.assertEqual(img.checksum, compute_checksum(io.BytesIO(b"photo-bytes-a")))

    def test_duplicate_upload_reuses_existing_photo(self):
        content = b"photo-bytes-dup"
        existing = baker.make(
            "dashboard.Image",
            pin=self.pin,
            profile=self.profile,
            visit=None,
            checksum=compute_checksum(io.BytesIO(content)),
        )
        request = self._post(photos=[SimpleUploadedFile("dup.jpg", content, content_type="image/jpeg")])
        uploaded = _sync_visit_photos(request, self.pin, self.visit)

        self.assertFalse(uploaded)
        existing.refresh_from_db()
        self.assertEqual(existing.visit_id, self.visit.pk)
        self.assertEqual(Image.objects.filter(pin=self.pin).count(), 1)

    def test_cannot_steal_another_visits_photo(self):
        other_visit = baker.make("dashboard.PinVisit", pin=self.pin, visited_at=timezone.now())
        theirs = baker.make("dashboard.Image", pin=self.pin, profile=self.profile, visit=other_visit)
        request = self._post(existing_ids=[theirs.pk])
        _sync_visit_photos(request, self.pin, self.visit)

        theirs.refresh_from_db()
        self.assertEqual(theirs.visit_id, other_visit.pk)

    def test_deselected_photo_is_detached(self):
        mine = baker.make("dashboard.Image", pin=self.pin, profile=self.profile, visit=self.visit)
        request = self._post()
        _sync_visit_photos(request, self.pin, self.visit)

        mine.refresh_from_db()
        self.assertIsNone(mine.visit_id)
