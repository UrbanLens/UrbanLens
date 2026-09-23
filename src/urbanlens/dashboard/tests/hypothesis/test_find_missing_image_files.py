"""``manage.py find_missing_image_files``: rows that name a stored file which is not there (P59)."""

from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_ORIGINAL = "pin_images/ok/photo.jpg"
_THUMB = "pin_images/thumbs/ok/photo-thumb.webp"
_MARKER = "pin_images/markers/ok/photo-marker.webp"
_GONE_THUMB = "pin_images/thumbs/gone/lightbox-associations-thumb.webp"
_GONE_ORIGINAL = "pin_images/gone/lightbox-associations.jpg"


def _jpeg_bytes() -> bytes:
    buf = BytesIO()
    PILImage.new("RGB", (64, 48), color=(90, 40, 10)).save(buf, format="JPEG")
    return buf.getvalue()


class FindMissingImageFilesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.media_root = Path(tempfile.mkdtemp(prefix="ul_missing_files_"))
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=str(self.media_root), MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        for name, data in ((_ORIGINAL, _jpeg_bytes()), (_THUMB, b"webp"), (_MARKER, b"webp")):
            (self.media_root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.media_root / name).write_bytes(data)
        profile = baker.make(User).profile
        self.healthy = baker.make(Image, profile=profile, image=_ORIGINAL, thumbnail=_THUMB, marker_thumbnail=_MARKER)
        self.thumb_gone = baker.make(
            Image, profile=profile, image=_ORIGINAL, thumbnail=_GONE_THUMB, marker_thumbnail=_MARKER
        )
        self.original_gone = baker.make(Image, profile=profile, image=_GONE_ORIGINAL, thumbnail=_THUMB)

    def run_command(self, *args: str) -> str:
        out = StringIO()
        call_command("find_missing_image_files", *args, stdout=out)
        return out.getvalue()

    def test_reports_each_row_and_column_naming_a_missing_file(self) -> None:
        output = self.run_command()

        self.assertIn(f"pk={self.thumb_gone.pk} thumbnail {_GONE_THUMB}", output)
        self.assertIn(f"pk={self.original_gone.pk} image {_GONE_ORIGINAL}", output)
        self.assertNotIn(f"pk={self.healthy.pk} ", output)

    def test_a_report_changes_nothing(self) -> None:
        with mock.patch(_ENQUEUE) as enqueue:
            self.run_command()

        enqueue.assert_not_called()
        self.thumb_gone.refresh_from_db()
        self.assertEqual(self.thumb_gone.thumbnail.name, _GONE_THUMB)

    def test_repair_regenerates_a_derived_copy_from_the_stored_original(self) -> None:
        with mock.patch(_ENQUEUE, side_effect=lambda task, ids: task(ids)):
            self.run_command("--repair")

        self.thumb_gone.refresh_from_db()
        regenerated = self.thumb_gone.thumbnail.name
        assert regenerated
        self.assertNotEqual(regenerated, _GONE_THUMB)
        self.assertTrue(self.thumb_gone.thumbnail.storage.exists(regenerated))
        self.assertEqual(self.thumb_gone.marker_thumbnail.name, _MARKER)
        self.assertEqual(self.run_command().count("pk="), 1, "only the row with no original is left")

    def test_repair_leaves_a_row_whose_original_is_gone(self) -> None:
        with mock.patch(_ENQUEUE) as enqueue:
            self.run_command("--repair")

        self.original_gone.refresh_from_db()
        self.assertEqual(self.original_gone.image.name, _GONE_ORIGINAL)
        self.assertNotIn(self.original_gone.pk, [pk for call in enqueue.call_args_list for pk in call.args[1]])

    def test_repair_skips_rows_that_are_not_photos_or_still_pending(self) -> None:
        profile = self.healthy.profile
        video = baker.make(Image, profile=profile, media_type=MediaKind.VIDEO, image=_ORIGINAL, thumbnail=_GONE_THUMB)
        pending = baker.make(Image, profile=profile, image=_ORIGINAL, thumbnail=_GONE_THUMB, pending_scan=True)

        with mock.patch(_ENQUEUE) as enqueue:
            self.run_command("--repair")

        queued = [pk for call in enqueue.call_args_list for pk in call.args[1]]
        self.assertEqual(queued, [self.thumb_gone.pk])
        for row in (video, pending):
            row.refresh_from_db()
            self.assertEqual(row.thumbnail.name, _GONE_THUMB)
