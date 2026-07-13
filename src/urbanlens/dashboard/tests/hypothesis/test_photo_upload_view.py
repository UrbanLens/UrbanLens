"""Tests for PhotoUploadView's content-type gate, including the video-uploads permission."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature

_UPLOAD_URL = reverse("memories.photos.upload")


class PhotoUploadViewContentTypeTests(TestCase):
    """POST /memories/photos/upload/ accepts images always, videos only when permitted."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin; keep it off the subject
        self.user: User = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)

    def test_image_upload_succeeds(self) -> None:
        image_file = SimpleUploadedFile("photo.jpg", b"photo-bytes", content_type="image/jpeg")
        response = self.client.post(_UPLOAD_URL, {"image": image_file})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Image.objects.filter(profile__user=self.user).exists())

    def test_non_media_upload_rejected(self) -> None:
        text_file = SimpleUploadedFile("notes.txt", b"not an image", content_type="text/plain")
        response = self.client.post(_UPLOAD_URL, {"image": text_file})
        self.assertEqual(response.status_code, 400)

    def test_video_upload_rejected_without_feature(self) -> None:
        video_file = SimpleUploadedFile("clip.mp4", b"video-bytes", content_type="video/mp4")
        response = self.client.post(_UPLOAD_URL, {"image": video_file})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Image.objects.filter(profile__user=self.user).exists())

    def test_video_upload_allowed_with_feature(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.VIDEO_UPLOADS)
        video_file = SimpleUploadedFile("clip.mp4", b"video-bytes", content_type="video/mp4")
        response = self.client.post(_UPLOAD_URL, {"image": video_file})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Image.objects.filter(profile__user=self.user).exists())
