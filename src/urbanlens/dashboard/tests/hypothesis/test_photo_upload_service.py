"""Behavioral lock on the Memories photo-upload path.

Written to characterize ``controllers.photos.PhotoUploadView.post`` *before*
its body was extracted into ``services.photo_upload.upload_photo``, and kept
afterwards so the extraction stays honest: the web uploader and the external
API's ``POST photos/`` now share one implementation, and this is what catches
that implementation drifting away from what the page has always done.

Every assertion here is about the HTMX/JSON contract the Memories page's
uploader JS depends on - the status codes in particular, since the page
distinguishes a duplicate (409) from a rejected file (400) from a quota
overrun (413) purely by status.
"""

from __future__ import annotations

import base64
from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.profile.model import Profile

_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class PhotoUploadViewTests(TestCase):
    """The Memories uploader's JSON contract, unchanged by the service extraction."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("memories.photos.upload")

    def _upload(self, name: str = "shot.png", content: bytes = _PNG_BYTES, content_type: str = "image/png"):
        """POST one file to the upload endpoint."""
        return self.client.post(self.url, {"image": SimpleUploadedFile(name, content, content_type=content_type)})

    def test_missing_file_is_a_400(self) -> None:
        """The page posts one file per request; none at all is a client error."""
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], "No image provided.")

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_successful_upload_returns_201_and_creates_the_row(self, mock_enqueue) -> None:
        """A valid image lands as an Image owned by the uploader, with ingestion queued."""
        response = self._upload()
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)

        image = Image.objects.get(profile=self.profile)
        self.assertEqual(image.media_type, MediaKind.PHOTO)
        self.assertIsNotNone(image.checksum)
        self.assertEqual(image.file_size, len(_PNG_BYTES))
        # Queued outside the upload lock, exactly once, with the new row's pk.
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.args[1], image.pk)

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_duplicate_upload_is_409(self, _mock_enqueue) -> None:
        """Re-uploading identical bytes is refused by checksum, per profile."""
        self._upload()
        response = self._upload(name="same-bytes-different-name.png")
        self.assertEqual(response.status_code, HTTPStatus.CONFLICT)
        self.assertEqual(response.json()["error"], "You already uploaded this file.")
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 1)

    @patch("urbanlens.dashboard.services.celery.safely_enqueue_task")
    def test_another_profile_may_upload_the_same_bytes(self, _mock_enqueue) -> None:
        """The duplicate check is per-profile, not global."""
        self._upload()

        other = baker.make(User)
        self.client.force_login(other)
        response = self._upload()
        self.assertEqual(response.status_code, HTTPStatus.CREATED)

    def test_unsupported_type_is_400(self) -> None:
        """Anything that isn't an image, video, or known document type is refused."""
        response = self._upload(name="notes.bin", content=b"not-an-image", content_type="application/octet-stream")
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], "That file is not an image, video, or supported document type.")

    def test_video_without_the_feature_is_403(self) -> None:
        """Video uploads stay behind their account feature gate."""
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=False):
            response = self._upload(name="clip.mp4", content=b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)
        self.assertEqual(response.json()["error"], "Video uploads are not enabled for your account.")
