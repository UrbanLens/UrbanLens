"""Hostile-input tests for uploaded import archives (services/import_data.py).

Covers the guards on `_extract_and_validate` (zip-slip path traversal -
including the sibling-directory-prefix variant a bare startswith check would
miss - and decompression-bomb ceilings) plus the trust boundaries inside the
importers themselves: connections.json must never forge friendship state the
importer couldn't create through the UI, and a foreign pin uuid in pins.json
must never map the importer's data onto another user's pin.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest import mock
import zipfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services import import_data
from urbanlens.dashboard.services.import_data import (
    ImportResult,
    _extract_and_validate,
    _import_connections,
    _import_pins,
    _ImportValidationError,
)
from urbanlens.dashboard.services.malware_scan import MalwareScanUnavailableError


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


class ExtractAndValidateSafetyTests(SimpleTestCase):
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
        with mock.patch.object(import_data, "_extraction_size_ceiling", return_value=10), self.assertRaises(_ImportValidationError) as ctx:
            self._run(_valid_entries())
        self.assertIn("too large", str(ctx.exception))

    def test_caps_admit_ordinary_archives(self) -> None:
        """The real ceilings are far above any legitimate export - a normal
        archive passes with the production values untouched."""
        self.assertTrue(self._run(_valid_entries()))

    def test_size_ceiling_tracks_the_storage_quota(self) -> None:
        """Export archives bundle real photo files, so the ceiling must sit
        ABOVE the user's storage quota (a 10 GB-quota user's legitimate
        archive would be refused by a fixed 2 GiB cap) while unlimited-quota
        users still get a finite bomb guard."""
        with mock.patch("urbanlens.dashboard.services.storage.get_quota_bytes", return_value=10 * 1024**3):
            self.assertEqual(import_data._extraction_size_ceiling(object()), 20 * 1024**3)
        with mock.patch("urbanlens.dashboard.services.storage.get_quota_bytes", return_value=None):
            unlimited_ceiling = import_data._extraction_size_ceiling(object())
        self.assertGreater(unlimited_ceiling, 10 * 1024**3)
        self.assertEqual(import_data._extraction_size_ceiling(None), import_data._EXTRACTED_BYTES_FLOOR * 32)

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


class ExtractedFileScanningTests(SimpleTestCase):
    """Every non-JSON file extracted from the archive is malware-scanned and
    content-sniffed before any importer (present or future) ever opens it -
    see _scan_extracted_files. clamav_enabled is forced False in the test
    settings (no real clamd daemon available here), so malware_error_for_upload
    is mocked directly to exercise the infected/unavailable branches; the
    content-type mismatch checks below run for real, against actual magic
    bytes, since that check needs no external service."""

    def _run(self, entries: dict[str, bytes]) -> str:
        with tempfile.TemporaryDirectory() as workdir:
            zip_path = os.path.join(workdir, "upload.zip")
            _write_zip(zip_path, entries)
            extract_dir = os.path.join(workdir, "out")
            return _extract_and_validate(zip_path, extract_dir, job_id="test-job")

    def test_clean_archive_with_a_real_photo_passes(self) -> None:
        entries = _valid_entries()
        # Real JPEG magic bytes (FF D8 FF) under a matching .jpg extension.
        entries["urbanlens_export_2026-07-18/photos/cover.jpg"] = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
        self.assertTrue(self._run(entries))

    def test_content_type_mismatch_is_rejected(self) -> None:
        """A file claiming to be a photo (.jpg extension) whose real bytes are
        a PDF (magic bytes %PDF) must be rejected - the same check that
        catches a spoofed direct upload via images.image_upload_error."""
        entries = _valid_entries()
        entries["urbanlens_export_2026-07-18/photos/cover.jpg"] = b"%PDF-1.4\n" + b"\x00" * 32
        with self.assertRaises(_ImportValidationError) as ctx:
            self._run(entries)
        self.assertIn("cover.jpg", str(ctx.exception))
        self.assertIn("doesn't match its file type", str(ctx.exception))

    def test_json_files_are_never_scanned_or_sniffed(self) -> None:
        """manifest.json/pins.json are the export's own structured data, not a
        user media upload - even with malware_error_for_upload mocked to flag
        everything, a JSON-only archive must still pass."""
        with mock.patch("urbanlens.dashboard.services.malware_scan.malware_error_for_upload", return_value="infected"):
            self.assertTrue(self._run(_valid_entries()))

    def test_infected_file_is_rejected(self) -> None:
        entries = _valid_entries()
        entries["urbanlens_export_2026-07-18/photos/cover.jpg"] = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
        with mock.patch("urbanlens.dashboard.services.malware_scan.malware_error_for_upload", return_value="This file was flagged as malicious"), self.assertRaises(_ImportValidationError) as ctx:
            self._run(entries)
        self.assertIn("cover.jpg", str(ctx.exception))
        self.assertIn("malicious", str(ctx.exception))

    def test_scanner_unavailable_is_a_retryable_error_not_a_permanent_rejection(self) -> None:
        entries = _valid_entries()
        entries["urbanlens_export_2026-07-18/photos/cover.jpg"] = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
        with mock.patch("urbanlens.dashboard.services.malware_scan.malware_error_for_upload", side_effect=MalwareScanUnavailableError("down")), self.assertRaises(_ImportValidationError) as ctx:
            self._run(entries)
        self.assertIn("temporarily unavailable", str(ctx.exception))
        self.assertNotIn("malicious", str(ctx.exception))


class ImportConnectionsTrustTests(TestCase):
    """connections.json rows are treated as requests, never as facts - an
    import must not create friendship state the importer couldn't create
    through the UI."""

    def setUp(self) -> None:
        super().setUp()
        self.importer = baker.make(User, username="importer").profile
        self.other = baker.make(User, username="victim").profile

    def _import_rows(self, rows: list[dict]) -> ImportResult:
        result = ImportResult()
        with tempfile.TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "connections.json"), "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            _import_connections(self.importer, data_dir, result, pin_uuid_map={}, label_uuid_map={})
        return result

    def _row(self, **overrides) -> dict:
        row = {
            "other_user_uuid": str(self.other.uuid),
            "other_username": "victim",
            "direction": "outgoing",
            "status": "Accepted",
        }
        row.update(overrides)
        return row

    def test_forged_incoming_accepted_row_creates_nothing(self) -> None:
        """The archive claiming another user befriended the importer must
        not materialize a row 'from' that user."""
        self._import_rows([self._row(direction="incoming")])
        self.assertFalse(Friendship.objects.exists())

    def test_outgoing_accepted_row_downgrades_to_a_request(self) -> None:
        """A crafted 'Accepted' status must not skip the recipient's consent."""
        self._import_rows([self._row(status="Accepted", permissions="View Friends")])
        friendship = Friendship.objects.get()
        self.assertEqual(friendship.from_profile_id, self.importer.pk)
        self.assertEqual(friendship.to_profile_id, self.other.pk)
        self.assertEqual(friendship.status, FriendshipStatus.REQUESTED)

    def test_outgoing_block_is_restored_as_a_block(self) -> None:
        """Blocking is unilateral and the importer's own action - it survives."""
        self._import_rows([self._row(status="Blocked")])
        friendship = Friendship.objects.get()
        self.assertEqual(friendship.from_profile_id, self.importer.pk)
        self.assertEqual(friendship.status, FriendshipStatus.BLOCKED)

    def test_existing_reverse_block_stops_a_new_request(self) -> None:
        """A user who blocked the importer cannot be re-requested via import."""
        Friendship.objects.create(from_profile=self.other, to_profile=self.importer, status=FriendshipStatus.BLOCKED)
        self._import_rows([self._row()])
        self.assertEqual(Friendship.objects.count(), 1)
        self.assertEqual(Friendship.objects.get().status, FriendshipStatus.BLOCKED)

    def test_anonymized_pending_rows_are_skipped(self) -> None:
        """Rows the export intentionally stripped identity from have nothing
        to act on."""
        result = self._import_rows([self._row(other_user_uuid=None, status="pending")])
        self.assertFalse(Friendship.objects.exists())
        self.assertEqual(result.skipped.get("connections"), 1)

    def test_self_referencing_row_is_skipped(self) -> None:
        result = self._import_rows([self._row(other_user_uuid=str(self.importer.uuid))])
        self.assertFalse(Friendship.objects.exists())
        self.assertEqual(result.skipped.get("connections"), 1)


class ImportPinUuidTrustTests(TestCase):
    """A foreign pin uuid in pins.json must neither block the import nor map
    the importer's data onto the other user's pin."""

    def test_foreign_pin_uuid_imports_as_a_fresh_pin(self) -> None:
        importer = baker.make(User, username="importer").profile
        victim_pin = baker.make(Pin, profile=baker.make(User, username="victim").profile, name="Victim pin")

        result = ImportResult()
        pin_uuid_map: dict[str, int] = {}
        rows = [{"uuid": str(victim_pin.uuid), "name": "Sneaky", "latitude": 42.65, "longitude": -73.75}]
        with tempfile.TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "pins.json"), "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            _import_pins(importer, data_dir, result, pin_uuid_map=pin_uuid_map, label_uuid_map={})

        # The map must point at the importer's new pin, never the victim's -
        # the visit-history step creates rows against these pks.
        mapped_pk = pin_uuid_map.get(str(victim_pin.uuid))
        self.assertIsNotNone(mapped_pk)
        self.assertNotEqual(mapped_pk, victim_pin.pk)
        imported = Pin.objects.get(pk=mapped_pk)
        self.assertEqual(imported.profile_id, importer.pk)
        self.assertNotEqual(imported.uuid, victim_pin.uuid)
