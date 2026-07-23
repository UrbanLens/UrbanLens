"""Tests that direct-message photo uploads flow through the shared photo pipeline.

Photos attached to DMs are uploads like any other: the upload endpoint must
queue ``process_image_upload`` (EXIF GPS/capture-time extraction, location
resolution, visit suggestion), and DM-attached images must appear in the
uploader's Memories gallery and organize queue rather than being excluded.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.memories.visits import maybe_suggest_photo_visit

_PIN_LAT = 40.0
_PIN_LNG = -74.0


def _jpeg_file(name: str = "photo.jpg") -> SimpleUploadedFile:
    """Build a small in-memory JPEG upload."""
    img = PILImage.new("RGB", (60, 40), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class DirectMessageImageUploadPipelineTests(TestCase):
    """POST /messages/upload-image/ queues metadata ingestion like every other upload path."""

    def setUp(self) -> None:
        super().setUp()
        self.me = baker.make("auth.User").profile
        self.client.force_login(self.me.user)

    def test_upload_enqueues_process_image_upload(self) -> None:
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue:
            response = self.client.post(reverse("messages.upload_image"), {"image": _jpeg_file()})

        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(pk=response.json()["id"])
        self.assertEqual(image.profile_id, self.me.pk)

        from urbanlens.dashboard.tasks import process_image_upload

        enqueue.assert_called_once_with(process_image_upload, image.pk)

    def test_uploaded_dm_photo_lands_in_memories_gallery_and_queue(self) -> None:
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task"):
            response = self.client.post(reverse("messages.upload_image"), {"image": _jpeg_file()})

        image = Image.objects.get(pk=response.json()["id"])
        self.assertIn(image, Image.objects.uploaded_by(self.me))
        self.assertIn(image, Image.objects.needs_attention(self.me))


class DirectMessageAttachedPhotoTests(TestCase):
    """Photos attached to a sent DM stay in the sender's Memories pipeline."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = baker.make("auth.User").profile
        self.recipient = baker.make("auth.User").profile
        self.message = baker.make("dashboard.DirectMessage", sender=self.sender, recipient=self.recipient, body="hi")

    def _dm_photo(self, **overrides) -> Image:
        fields = {
            "pin": None,
            "wiki": None,
            "profile": self.sender,
            "direct_message": self.message,
            **overrides,
        }
        return baker.make("dashboard.Image", **fields)

    def test_dm_attached_photo_appears_in_gallery_and_attention_queue(self) -> None:
        photo = self._dm_photo()
        self.assertIn(photo, Image.objects.uploaded_by(self.sender))
        self.assertIn(photo, Image.objects.needs_attention(self.sender))

    def test_geotagged_dm_photo_near_own_pin_raises_visit_suggestion(self) -> None:
        location = baker.make("dashboard.Location", latitude=str(_PIN_LAT), longitude=str(_PIN_LNG))
        baker.make("dashboard.Pin", profile=self.sender, location=location)
        photo = self._dm_photo(
            latitude=Decimal(str(_PIN_LAT + 0.0003)),
            longitude=Decimal(str(_PIN_LNG + 0.0003)),
            taken_at=timezone.make_aware(datetime.datetime(2026, 6, 1, 12, 0, 0)),
        )

        suggestion = maybe_suggest_photo_visit(photo)

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion.origin_image_id, photo.pk)
        self.assertEqual(suggestion.location_id, location.pk)
