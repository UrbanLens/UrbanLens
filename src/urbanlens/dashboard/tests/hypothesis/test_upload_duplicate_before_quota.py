"""A re-upload of a file the profile already stored is a duplicate, not a quota overrun.

It stores no new bytes, so a full quota must not change the answer from 409 to 413. The quota is
reserved only once the duplicate lookup, inside the same reservation, has found nothing.
"""

from __future__ import annotations

import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.media.storage import GIB


def _png() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (1, 1), (0, 128, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


_PNG = _png()


def _upload(name: str = "again.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _PNG, content_type="image/png")


class DuplicateAtFullQuotaTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        site = SiteSettings.get_current()
        site.storage_quota_gb = 1
        site.save()

    def fill_quota(self) -> None:
        baker.make(Image, profile=self.profile, file_size=GIB)

    def test_the_vault_service(self) -> None:
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo

        upload_photo(self.profile, _upload("first.png"))
        self.fill_quota()

        with self.assertRaises(PhotoUploadError) as caught:
            upload_photo(self.profile, _upload())
        self.assertEqual(caught.exception.status, 409)

    def test_the_pin_upload_view(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
        url = reverse("pin.upload_image", args=[pin.slug])
        self.assertEqual(self.client.post(url, {"image": _upload("first.png")}).status_code, 200)
        self.fill_quota()

        self.assertEqual(self.client.post(url, {"image": _upload()}).status_code, 409)

    def test_the_safety_gallery_view(self) -> None:
        checkin = baker.make(SafetyCheckin, profile=self.profile, title="Hike")
        url = reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug})
        self.assertEqual(self.client.post(url, {"image": _upload("first.png")}).status_code, 201)
        self.fill_quota()

        self.assertEqual(self.client.post(url, {"image": _upload()}).status_code, 409)

    def test_the_photo_scan_upload_view(self) -> None:
        from urbanlens.dashboard.models.pin_suggestions.model import (
            PinSuggestion,
            PinSuggestionOrigin,
            PinSuggestionStatus,
        )

        suggestion = baker.make(
            PinSuggestion,
            profile=self.profile,
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            status=PinSuggestionStatus.PENDING,
        )
        url = reverse("tools.photo_scan.upload_photo")
        data = {"suggestion_id": str(suggestion.pk)}
        self.assertEqual(self.client.post(url, {**data, "image": _upload("first.png")}).status_code, 201)
        self.fill_quota()

        self.assertEqual(self.client.post(url, {**data, "image": _upload()}).status_code, 409)
