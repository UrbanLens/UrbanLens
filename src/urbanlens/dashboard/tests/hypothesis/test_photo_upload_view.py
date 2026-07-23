"""Tests for PhotoUploadView's content-type gate: images, videos, and documents."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature

_UPLOAD_URL = reverse("memories.photos.upload")


def _grant_feature(*features: str) -> None:
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=",".join(features))


class PhotoUploadViewContentTypeTests(TestCase):
    """POST /memories/photos/upload/ accepts images always, videos/documents only when permitted."""

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
        image = Image.objects.get(profile__user=self.user)
        self.assertEqual(image.media_type, MediaKind.PHOTO)

    def test_genuinely_unsupported_upload_rejected(self) -> None:
        archive_file = SimpleUploadedFile("archive.zip", b"not media", content_type="application/zip")
        response = self.client.post(_UPLOAD_URL, {"image": archive_file})
        self.assertEqual(response.status_code, 400)

    def test_video_upload_rejected_without_feature(self) -> None:
        video_file = SimpleUploadedFile("clip.mp4", b"video-bytes", content_type="video/mp4")
        response = self.client.post(_UPLOAD_URL, {"image": video_file})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Image.objects.filter(profile__user=self.user).exists())

    def test_video_upload_allowed_with_feature(self) -> None:
        _grant_feature(SiteFeature.VIDEO_UPLOADS)
        video_file = SimpleUploadedFile("clip.mp4", b"video-bytes", content_type="video/mp4")
        response = self.client.post(_UPLOAD_URL, {"image": video_file})
        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(profile__user=self.user)
        self.assertEqual(image.media_type, MediaKind.VIDEO)

    def test_document_upload_rejected_without_feature(self) -> None:
        doc_file = SimpleUploadedFile("notes.docx", b"doc-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        response = self.client.post(_UPLOAD_URL, {"image": doc_file})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Image.objects.filter(profile__user=self.user).exists())

    def test_document_upload_allowed_with_feature(self) -> None:
        _grant_feature(SiteFeature.DOCUMENT_UPLOADS)
        doc_file = SimpleUploadedFile("notes.docx", b"doc-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        response = self.client.post(_UPLOAD_URL, {"image": doc_file})
        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(profile__user=self.user)
        self.assertEqual(image.media_type, MediaKind.DOCUMENT)

    def test_pdf_upload_detected_by_content_type(self) -> None:
        _grant_feature(SiteFeature.DOCUMENT_UPLOADS)
        pdf_file = SimpleUploadedFile("report", b"%PDF-1.4 fake", content_type="application/pdf")
        response = self.client.post(_UPLOAD_URL, {"image": pdf_file})
        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(profile__user=self.user)
        self.assertEqual(image.media_type, MediaKind.DOCUMENT)

    def test_txt_upload_requires_document_feature(self) -> None:
        # .txt is a supported document extension now, not a generic rejection.
        txt_file = SimpleUploadedFile("notes.txt", b"plain text", content_type="text/plain")
        response = self.client.post(_UPLOAD_URL, {"image": txt_file})
        self.assertEqual(response.status_code, 403)

    def test_upload_over_max_file_size_rejected(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(max_upload_file_size_mb=1)
        oversized = SimpleUploadedFile("photo.jpg", b"x" * (2 * 1_000_000), content_type="image/jpeg")
        response = self.client.post(_UPLOAD_URL, {"image": oversized})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Image.objects.filter(profile__user=self.user).exists())

    def test_upload_within_max_file_size_allowed(self) -> None:
        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(max_upload_file_size_mb=1)
        small_file = SimpleUploadedFile("photo.jpg", b"x" * 1000, content_type="image/jpeg")
        response = self.client.post(_UPLOAD_URL, {"image": small_file})
        self.assertEqual(response.status_code, 201)
