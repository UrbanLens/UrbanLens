"""Renaming a stored photo never leaves a row pointing at a file that is not there."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image

_OLD = "pin_images/ab/IMG_2041-private-name.jpg"


class AnonymizeFilenamesCrashSafetyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="ul_anonymize_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        settings_override = override_settings(MEDIA_ROOT=str(self.root))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        (self.root / _OLD).parent.mkdir(parents=True)
        (self.root / _OLD).write_bytes(b"jpeg-bytes")
        self.image = baker.make(Image, image=_OLD, original_filename="", thumbnail=None)

    def _run(self) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("anonymize_stored_photo_filenames", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _files(self) -> set[str]:
        return {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}

    def test_a_clean_run_renames_the_file_and_the_row_together(self) -> None:
        self._run()

        self.image.refresh_from_db()
        self.assertNotEqual(self.image.image.name, _OLD)
        self.assertNotIn("private-name", self.image.image.name)
        self.assertEqual(self.image.original_filename, "IMG_2041-private-name.jpg")
        self.assertEqual(self._files(), {self.image.image.name})
        self.assertEqual((self.root / self.image.image.name).read_bytes(), b"jpeg-bytes")

    def test_a_failed_database_update_leaves_the_row_and_its_file_alone(self) -> None:
        """Moving first and updating second left the row naming a file that had already gone."""
        with mock.patch("django.db.models.query.QuerySet.update", side_effect=DatabaseError("connection lost")):
            _out, err = self._run()

        self.image.refresh_from_db()
        self.assertEqual(self.image.image.name, _OLD)
        self.assertEqual(self._files(), {_OLD}, "the row's file is intact and no copy is left behind")
        self.assertIn("DB update failed", err)

    def test_a_failed_copy_changes_nothing(self) -> None:
        with mock.patch("shutil.copy2", side_effect=OSError("disk full")):
            _out, err = self._run()

        self.image.refresh_from_db()
        self.assertEqual(self.image.image.name, _OLD)
        self.assertEqual(self._files(), {_OLD})
        self.assertIn("FAILED", err)

    def test_an_old_file_that_cannot_be_removed_is_a_stray_not_a_broken_row(self) -> None:
        with mock.patch.object(FileSystemStorage, "delete", side_effect=OSError("read-only")):
            _out, err = self._run()

        self.image.refresh_from_db()
        self.assertNotEqual(self.image.image.name, _OLD)
        self.assertEqual(self._files(), {_OLD, self.image.image.name})
        self.assertIn("could not be removed", err)

    def test_a_commit_whose_acknowledgement_is_lost_keeps_the_copy_the_rows_now_name(self) -> None:
        """A DatabaseError does not prove the update failed: the server may have committed before the connection dropped."""
        real_atomic = transaction.atomic

        class _AtomicThatLosesItsAcknowledgement:
            def __init__(self, *args, **kwargs) -> None:
                self.inner = real_atomic(*args, **kwargs)

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *exc_info):
                self.inner.__exit__(*exc_info)
                raise DatabaseError("connection lost after the commit")

        with mock.patch.object(transaction, "atomic", _AtomicThatLosesItsAcknowledgement):
            self._run()

        self.image.refresh_from_db()
        self.assertNotEqual(self.image.image.name, _OLD)
        self.assertEqual(
            self._files(), {self.image.image.name}, "the row names a file that exists, and the original is gone"
        )

    def test_a_failed_database_update_that_cannot_be_read_back_removes_nothing(self) -> None:
        with (
            mock.patch("django.db.models.query.QuerySet.update", side_effect=DatabaseError("connection lost")),
            mock.patch("django.db.models.query.QuerySet.exists", side_effect=DatabaseError("still lost")),
        ):
            _out, err = self._run()

        self.assertEqual(
            len(self._files()), 2, "the original and the copy are both kept when nothing can say which the row names"
        )
        self.assertIn("could not be read back", err)
