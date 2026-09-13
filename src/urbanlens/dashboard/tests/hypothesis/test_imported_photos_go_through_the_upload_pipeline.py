"""Photos and map overlay images restored from an export archive go through the upload pipeline (P119).

Both were written straight into storage and published, so a photo came back with whatever metadata it was archived
with, and nothing re-encoded it. An upload through the site is stored pending and processed by the sandbox worker;
an import is the same bytes arriving by another route.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import _fixtures

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class _ImportCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.importer = baker.make(User).profile
        self.archive = Path(tempfile.mkdtemp(prefix="ul_import_pipeline_"))
        self.addCleanup(shutil.rmtree, self.archive, ignore_errors=True)
        self.name, self.data = _fixtures()["jpeg-exif-gps"]

    def _archive_file(self, directory: str) -> None:
        (self.archive / directory).mkdir(parents=True, exist_ok=True)
        (self.archive / directory / self.name).write_bytes(self.data)

    def assertWaitsForThePipeline(self, image: Image, enqueue: mock.MagicMock) -> None:
        from urbanlens.dashboard import tasks

        image.refresh_from_db()
        self.assertTrue(image.pending_scan, "the imported file was published as it was archived")
        self.assertIn(mock.call(tasks.process_image_upload, image.pk), enqueue.call_args_list)


class AnImportedPhotoTests(_ImportCase):
    def test_an_imported_photo_waits_for_the_upload_pipeline(self) -> None:
        self._archive_file("photos")
        (self.archive / "photos" / "metadata.json").write_text(
            json.dumps([{"filename": self.name, "media_type": "photo"}])
        )

        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE) as enqueue:
            import_data._import_photos(
                self.importer, str(self.archive), import_data.ImportResult(), pin_uuid_map={}, label_uuid_map={}
            )

        self.assertWaitsForThePipeline(Image.objects.get(profile=self.importer), enqueue)


class AnImportedOverlayImageTests(_ImportCase):
    def test_an_imported_overlay_image_waits_for_the_upload_pipeline(self) -> None:
        self._archive_file(import_data.MapAnnotationsExportDirName)
        ctx = import_data.ImportContext(
            profile=self.importer,
            data_dir=str(self.archive),
            result=import_data.ImportResult(),
            pin_uuid_map={},
            label_uuid_map={},
        )

        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE) as enqueue:
            image = import_data.MapAnnotationsImport()._restore_overlay_image({"filename": self.name}, ctx)

        self.assertIsNotNone(image, "nothing was restored, so this proves nothing")
        self.assertWaitsForThePipeline(image, enqueue)
