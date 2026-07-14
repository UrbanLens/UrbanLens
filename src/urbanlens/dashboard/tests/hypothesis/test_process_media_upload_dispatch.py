"""Tests for process_image_upload's media-type dispatch and shared tail.

Verifies that photo/video/document uploads each get their own type-specific
extraction/downscaling step, but all funnel into the SAME shared tail:
resolving `location`, raising a visit suggestion, and recording file_size -
this is the "reuse the exact same code, no duplication" requirement for
PinSuggestion/VisitSuggestion creation. The type-specific service calls
(ffmpeg/soffice/tesseract) are mocked; only the dispatch/reuse logic is
under test here (see test_video_processing.py / test_document_processing.py
for the service-level logic itself).

Every call to process_image_upload() also mocks update_task_progress, same
as test_image_attribution.py - calling a bound task directly (rather than via
.delay()/.apply()) leaves self.request.id unset, and update_task_progress's
task.update_state() would otherwise hit the real (Redis) result backend with
an empty task_id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.tasks import generate_image_keywords, process_image_upload


class VideoUploadDispatchTests(TestCase):
    """A video upload runs ffprobe/ffmpeg logic, then the shared photo-visit tail."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        location = baker.make(Location, latitude=40.6892, longitude=-74.0445)
        self.pin = baker.make(Pin, profile=self.profile, location=location)
        self.image = baker.make(
            Image,
            profile=self.profile,
            pin=self.pin,
            media_type=MediaKind.VIDEO,
            image=SimpleUploadedFile("clip.mp4", b"fake-video-bytes", content_type="video/mp4"),
        )

    def test_video_metadata_feeds_shared_visit_suggestion_logic(self) -> None:
        metadata = {
            "taken_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            "latitude": 40.6892,
            "longitude": -74.0445,
        }
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.videos.process_uploaded_video", return_value=(metadata, None)),
        ):
            result = process_image_upload(self.image.pk)

        self.assertTrue(result)
        self.image.refresh_from_db()
        self.assertEqual(self.image.taken_at, metadata["taken_at"])
        self.assertEqual(float(self.image.latitude), 40.6892)
        self.assertEqual(float(self.image.longitude), -74.0445)
        # Same VisitSuggestion mechanism photos use - proves the tail is shared.
        self.assertTrue(VisitSuggestion.objects.filter(location=self.pin.location, origin_image=self.image).exists())

    def test_video_downscale_updates_file_size(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.videos.process_uploaded_video", return_value=({}, 12345)),
        ):
            process_image_upload(self.image.pk)
        self.image.refresh_from_db()
        self.assertEqual(self.image.file_size, 12345)

    def test_video_upload_does_not_enqueue_photo_keyword_generation(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.videos.process_uploaded_video", return_value=({}, None)),
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue,
        ):
            process_image_upload(self.image.pk)
        for call in mock_enqueue.call_args_list:
            self.assertNotEqual(call.args[0] if call.args else None, generate_image_keywords)


class DocumentUploadDispatchTests(TestCase):
    """A document upload runs the PDF-conversion/OCR branch, then the shared tail."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.image = baker.make(
            Image,
            profile=self.profile,
            media_type=MediaKind.DOCUMENT,
            image=SimpleUploadedFile("notes.docx", b"doc-bytes", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )

    def test_conversion_and_ocr_are_invoked_and_persisted(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.documents.convert_to_pdf", return_value=999) as mock_convert,
            patch("urbanlens.dashboard.services.documents.extract_pdf_text", return_value="Extracted document text") as mock_ocr,
        ):
            result = process_image_upload(self.image.pk)

        self.assertTrue(result)
        mock_convert.assert_called_once()
        mock_ocr.assert_called_once()
        self.image.refresh_from_db()
        self.assertEqual(self.image.file_size, 999)
        self.assertEqual(self.image.ocr_text, "Extracted document text")

    def test_no_ocr_text_leaves_field_unset(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.documents.convert_to_pdf", return_value=None),
            patch("urbanlens.dashboard.services.documents.extract_pdf_text", return_value=None),
        ):
            process_image_upload(self.image.pk)
        self.image.refresh_from_db()
        self.assertIsNone(self.image.ocr_text)

    def test_document_upload_does_not_enqueue_photo_keyword_generation(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.documents.convert_to_pdf", return_value=None),
            patch("urbanlens.dashboard.services.documents.extract_pdf_text", return_value=None),
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue,
        ):
            process_image_upload(self.image.pk)
        for call in mock_enqueue.call_args_list:
            self.assertNotEqual(call.args[0] if call.args else None, generate_image_keywords)


class PhotoUploadStillEnqueuesKeywordsTests(TestCase):
    """Sanity check that the photo branch is unaffected: it still runs keyword generation."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_photo_upload_enqueues_photo_keyword_generation(self) -> None:
        image = baker.make(
            Image,
            profile=self.profile,
            media_type=MediaKind.PHOTO,
            image=SimpleUploadedFile("photo.jpg", b"jpeg-bytes", content_type="image/jpeg"),
        )
        with (
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue,
        ):
            process_image_upload(image.pk)
        enqueued_tasks = [call.args[0] for call in mock_enqueue.call_args_list if call.args]
        self.assertIn(generate_image_keywords, enqueued_tasks)
