"""Files under ``pin_images/`` that no Image row names are found, reported, and removed only when asked (P14)."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import time
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image

_OLD = 60 * 24 * 3600


def _file(directory: str, *, age: float = _OLD) -> str:
    name = default_storage.save(f"{directory}/{uuid.uuid4().hex[:8]}/{uuid.uuid4().hex}.jpg", ContentFile(b"pixels"))
    past = time.time() - age
    os.utime(default_storage.path(name), (past, past))
    return name


class UnnamedPinImageSweepTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_pin_image_sweep_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=media_root)
        media.enable()
        self.addCleanup(media.disable)

    def _sweep(self, *args: str) -> str:
        out = io.StringIO()
        call_command("sweep_unnamed_pin_images", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_reports_and_deletes_nothing(self) -> None:
        orphan = _file("pin_images/analysis")

        report = self._sweep()

        self.assertTrue(default_storage.exists(orphan))
        self.assertIn("Found 1 unnamed file", report)

    def test_delete_removes_only_files_no_row_names(self) -> None:
        kept = {
            "image": _file("pin_images"),
            "thumbnail": _file("pin_images/thumbs"),
            "marker_thumbnail": _file("pin_images/markers"),
            "analysis_thumbnail": _file("pin_images/analysis"),
        }
        image = baker.make(Image, image="placeholder.jpg")
        Image.objects.filter(pk=image.pk).update(**kept)
        orphans = [
            _file("pin_images"),
            _file("pin_images/thumbs"),
            _file("pin_images/markers"),
            _file("pin_images/analysis"),
        ]

        self._sweep("--delete")

        for name in kept.values():
            self.assertTrue(default_storage.exists(name), name)
        for name in orphans:
            self.assertFalse(default_storage.exists(name), name)

    def test_a_file_whose_row_may_not_have_committed_yet_is_kept(self) -> None:
        fresh = _file("pin_images", age=60)

        self._sweep("--delete")

        self.assertTrue(default_storage.exists(fresh))

    def test_a_file_an_undo_could_restore_is_kept(self) -> None:
        from urbanlens.dashboard.models.undo.model import UndoAction

        restorable = _file("pin_images")
        baker.make(UndoAction, payload={"rows": [{"image": restorable}]})

        self._sweep("--delete")

        self.assertTrue(default_storage.exists(restorable))

    def test_files_outside_pin_images_are_not_touched(self) -> None:
        elsewhere = _file("avatars")

        self._sweep("--delete")

        self.assertTrue(default_storage.exists(elsewhere))
