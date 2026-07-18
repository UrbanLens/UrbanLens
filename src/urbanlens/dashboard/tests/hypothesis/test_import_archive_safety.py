"""Extraction-safety tests for uploaded import archives (services/import_data.py).

Covers the hostile-input guards on `_extract_and_validate`: zip-slip path
traversal (including the sibling-directory-prefix variant a bare startswith
check would miss), decompression-bomb ceilings, and that a well-formed
archive still extracts and resolves its data directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest import mock
import zipfile

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services import import_data
from urbanlens.dashboard.services.import_data import (
    _extract_and_validate,
    _ImportValidationError,
)


def _write_zip(zip_path: str, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def _valid_entries() -> dict[str, bytes]:
    manifest = json.dumps({"format": "urbanlens_v1", "contents": ["pins"]}).encode()
    return {
        "urbanlens_export_2026-07-18/manifest.json": manifest,
        "urbanlens_export_2026-07-18/pins.json": b"[]",
    }


class ExtractAndValidateSafetyTests(TestCase):
    """Hostile archives are rejected before extraction; valid ones still work."""

    def _run(self, entries: dict[str, bytes], extract_dirname: str = "job1") -> str:
        with tempfile.TemporaryDirectory() as workdir:
            zip_path = os.path.join(workdir, "upload.zip")
            _write_zip(zip_path, entries)
            extract_dir = os.path.join(workdir, extract_dirname)
            return _extract_and_validate(zip_path, extract_dir, job_id="test-job")

    def test_valid_archive_extracts_and_finds_the_data_dir(self) -> None:
        data_dir = self._run(_valid_entries())
        self.assertTrue(data_dir.endswith("urbanlens_export_2026-07-18"))

    def test_parent_directory_traversal_is_rejected(self) -> None:
        entries = _valid_entries()
        entries["../escaped.txt"] = b"pwned"
        with self.assertRaises(_ImportValidationError):
            self._run(entries)

    def test_sibling_prefix_escape_is_rejected(self) -> None:
        """The variant a bare startswith(extract_dir) check accepts: an entry
        resolving into a SIBLING directory whose name shares the extract
        dir's prefix ("job1" vs "job1evil")."""
        entries = _valid_entries()
        entries["../job1evil/escaped.txt"] = b"pwned"
        with self.assertRaises(_ImportValidationError):
            self._run(entries, extract_dirname="job1")

    def test_absolute_path_member_is_rejected_or_contained(self) -> None:
        """Absolute members must never land outside the extract dir. zipfile
        strips the leading slash on extraction, so either rejection or
        in-dir containment is acceptable - what matters is no file appears
        at the absolute path."""
        entries = _valid_entries()
        entries["/tmp/absolute-escape.txt"] = b"pwned"
        try:
            self._run(entries)
        except _ImportValidationError:
            pass
        self.assertFalse(os.path.exists("/tmp/absolute-escape.txt"))

    def test_too_many_members_is_rejected(self) -> None:
        with mock.patch.object(import_data, "_MAX_ARCHIVE_MEMBERS", 1), self.assertRaises(_ImportValidationError) as ctx:
            self._run(_valid_entries())
        self.assertIn("too many files", str(ctx.exception))

    def test_declared_size_over_ceiling_is_rejected(self) -> None:
        """A decompression bomb must be refused from its declared sizes alone,
        before any bytes are written to disk."""
        with mock.patch.object(import_data, "_MAX_EXTRACTED_BYTES", 10), self.assertRaises(_ImportValidationError) as ctx:
            self._run(_valid_entries())
        self.assertIn("too large", str(ctx.exception))

    def test_caps_admit_ordinary_archives(self) -> None:
        """The real ceilings are far above any legitimate JSON export - a
        normal archive passes with the production values untouched."""
        self.assertTrue(self._run(_valid_entries()))

    def test_not_a_zip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            zip_path = os.path.join(workdir, "upload.zip")
            with open(zip_path, "wb") as fh:
                fh.write(b"this is not a zip archive")
            with self.assertRaises(_ImportValidationError):
                _extract_and_validate(zip_path, os.path.join(workdir, "out"), job_id="test-job")

    def test_missing_upload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as workdir, self.assertRaises(_ImportValidationError):
            _extract_and_validate(os.path.join(workdir, "gone.zip"), os.path.join(workdir, "out"), job_id="test-job")
