"""Processing replaces an upload's stored file; the row must name the successor first."""

from __future__ import annotations

import io
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.media.images import discard_superseded_file, downscale_stored_image


def _jpeg_bytes(width: int = 900, height: int = 700) -> bytes:
    """A JPEG large enough that the pipeline rewrites it."""
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(120, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class DownscaleDefersTheDeleteTests(TestCase):
    """The unit-level half: the replacement names the superseded file, it does not delete it."""

    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_replaced_last_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.image = Image.objects.create(
            image=SimpleUploadedFile("photo.jpg", _jpeg_bytes(), content_type="image/jpeg"),
            profile=baker.make(User).profile,
        )

    def test_the_superseded_file_survives_the_replacement(self) -> None:
        old_name = self.image.image.name

        replacement = downscale_stored_image(self.image, max_dimension=None, convert_webp=True)

        assert replacement is not None
        self.assertNotEqual(self.image.image.name, old_name, "nothing was replaced, so this proves nothing")
        self.assertEqual(replacement.superseded_name, old_name)
        self.assertTrue(self.image.image.storage.exists(old_name), "deleted before the row could name its successor")

    def test_discarding_it_afterwards_removes_it(self) -> None:
        old_name = self.image.image.name
        replacement = downscale_stored_image(self.image, max_dimension=None, convert_webp=True)
        assert replacement is not None
        self.image.save(update_fields=["image"])

        discard_superseded_file(self.image, replacement.superseded_name)

        self.assertFalse(self.image.image.storage.exists(old_name))

    def test_a_file_another_row_still_points_at_is_kept(self) -> None:
        """Pin sharing points two rows at one key; discarding must not blank the other."""
        old_name = self.image.image.name
        sibling = Image.objects.create(profile=baker.make(User).profile)
        Image.objects.filter(pk=sibling.pk).update(image=old_name)

        replacement = downscale_stored_image(self.image, max_dimension=None, convert_webp=True)
        assert replacement is not None
        self.image.save(update_fields=["image"])
        discard_superseded_file(self.image, replacement.superseded_name)

        self.assertTrue(self.image.image.storage.exists(old_name), "the row that still points at this file lost it")

    def test_discarding_nothing_is_a_no_op(self) -> None:
        discard_superseded_file(self.image, None)
        self.assertTrue(self.image.image.storage.exists(self.image.image.name))


class ProcessingDeletesTheOldFileLastTests(TestCase):
    """The ordering, observed through the task that actually runs it."""

    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_replaced_last_task_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        site_settings = SiteSettings.get_current()
        site_settings.image_convert_webp = True
        site_settings.save(update_fields=["image_convert_webp"])
        self.image = Image.objects.create(
            image=SimpleUploadedFile("photo.jpg", _jpeg_bytes(), content_type="image/jpeg"),
            profile=baker.make(User).profile,
            pending_scan=True,
        )

    def _run_recording_deletes(self) -> list[tuple[str, str | None]]:
        """Run the upload task, recording what the row named as each file was deleted."""
        from urbanlens.dashboard.tasks import process_image_upload

        image_pk = self.image.pk
        observed: list[tuple[str, str | None]] = []
        original_delete = FileSystemStorage.delete

        def recording_delete(storage: FileSystemStorage, name: str):
            observed.append((name, Image.objects.filter(pk=image_pk).values_list("image", flat=True).first()))
            return original_delete(storage, name)

        with patch.object(FileSystemStorage, "delete", recording_delete):
            process_image_upload(image_pk)
        return observed

    def test_nothing_is_deleted_while_the_row_still_names_it(self) -> None:
        old_name = self.image.image.name

        observed = self._run_recording_deletes()

        self.image.refresh_from_db()
        self.assertNotEqual(self.image.image.name, old_name, "nothing was replaced, so this proves nothing")
        self.assertTrue([name for name, _ in observed if name == old_name], "the replaced file was never cleaned up")
        for name, named_by_row in observed:
            self.assertNotEqual(
                name,
                named_by_row,
                "deleted while the database still named it - every request for it in this window is authorized and then missing",
            )

    def test_the_replaced_file_is_gone_by_the_end(self) -> None:
        """Deferring the delete must not become leaking the file."""
        old_name = self.image.image.name

        self._run_recording_deletes()

        self.image.refresh_from_db()
        self.assertFalse(self.image.image.storage.exists(old_name))
        self.assertTrue(self.image.image.storage.exists(self.image.image.name))
