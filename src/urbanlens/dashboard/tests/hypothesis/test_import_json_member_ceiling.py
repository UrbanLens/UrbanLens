"""No data file in an import archive is parsed whole past a per-file ceiling.

``json.load`` builds Python objects about twice the size of the indented JSON an export writes, and the only bound was
the archive's extracted total, ``max(2 GiB, 2 x quota)``, so one ``pins.json`` could take several gigabytes of a sandbox
worker's memory.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from unittest import mock
import zipfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.import_export import import_data

_CEILING = 2048


def _entries(pins: bytes) -> dict[str, bytes]:
    manifest = json.dumps({"format": "urbanlens_v1", "contents": ["pins"]}).encode()
    return {"urbanlens_export_2026-10-07/manifest.json": manifest, "urbanlens_export_2026-10-07/pins.json": pins}


def _write_zip(zip_path: str, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


@mock.patch.object(import_data, "_MAX_JSON_MEMBER_BYTES", _CEILING)
class AnOversizedDataFileTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.enterContext(tempfile.TemporaryDirectory())
        self.zip_path = os.path.join(self.workdir, "upload.zip")
        baker.make(User)
        self.user = baker.make(User)

    def test_the_import_is_refused_with_a_clear_message_and_nothing_parses_it(self) -> None:
        _write_zip(self.zip_path, _entries(b"[" + b"{}," * _CEILING + b"{}]"))

        with (
            mock.patch.object(import_data.json, "load", wraps=json.load) as load,
            mock.patch.object(import_data, "schedule_import_cleanup"),
        ):
            self.assertFalse(import_data.run_import(self.user.pk, self.zip_path, "job-oversized"))

        status = import_data.ImportJobStatus("job-oversized").read()
        self.assertEqual(status["status"], "error")
        self.assertIn("larger than", status["message"])
        self.assertIn("MB", status["message"])
        self.assertEqual(load.call_count, 0, "a data file over the ceiling was parsed")

    def test_a_data_file_at_the_ceiling_still_imports(self) -> None:
        _write_zip(self.zip_path, _entries(b"[" + b" " * (_CEILING - 2) + b"]"))

        with mock.patch.object(import_data, "schedule_import_cleanup"):
            self.assertTrue(import_data.run_import(self.user.pk, self.zip_path, "job-at-ceiling"))

    def test_a_photo_over_the_json_ceiling_is_not_its_business(self) -> None:
        """The ceiling is for what ``json.load`` reads; photos have the archive's own byte ceiling."""
        entries = _entries(b"[]")
        entries["urbanlens_export_2026-10-07/notes.csv"] = b"x" * (_CEILING + 1)
        _write_zip(self.zip_path, entries)

        with mock.patch.object(import_data, "schedule_import_cleanup"):
            self.assertTrue(import_data.run_import(self.user.pk, self.zip_path, "job-csv"))


@mock.patch.object(import_data, "_MAX_JSON_MEMBER_BYTES", _CEILING)
class TheCeilingIsEnforcedWhileReadingTests(SimpleTestCase):
    """A declared size is checked first, but extraction counts what it actually writes."""

    def test_a_member_yielding_more_than_it_declares_is_refused(self) -> None:
        member = zipfile.ZipInfo("export/pins.json")
        member.file_size = 10
        archive = mock.Mock()
        archive.open.return_value = io.BytesIO(b"[" + b"0," * _CEILING + b"0]")

        with tempfile.TemporaryDirectory() as root, self.assertRaises(import_data._ImportMemberTooLargeError):  # noqa: SLF001 - the error the import reports
            import_data._extract_zip_members_bounded(archive, [member], os.path.realpath(root), 10 * _CEILING)  # noqa: SLF001 - the loop under test

    def test_a_file_on_disk_over_the_ceiling_is_not_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "pins.json"), "w", encoding="utf-8") as handle:
                handle.write("[" + "0," * _CEILING + "0]")

            with (
                mock.patch.object(import_data.json, "load") as load,
                self.assertRaises(import_data._ImportMemberTooLargeError),
            ):  # noqa: SLF001
                import_data._read_json(data_dir, "pins.json")  # noqa: SLF001 - every importer reads through it

        load.assert_not_called()
