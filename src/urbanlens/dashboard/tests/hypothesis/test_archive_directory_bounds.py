"""An archive's directory is bounded before it is read, not only its supported entries (P95).

``zipfile`` reads a ZIP's whole central directory into a ``ZipInfo`` per entry before anything looks at one: a 94 MB
upload of a million empty entries cost 586 MiB to open, and the 200 MB upload cap allows several times that.
``tarfile`` keeps every member's ``TarInfo``. The extraction budget counted only supported entries, once the directory
was built.
"""

from __future__ import annotations

import io
import os
import struct
import tarfile
import tempfile
import tracemalloc
from unittest import mock
import zipfile

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_export import archive_extractor, import_data
from urbanlens.dashboard.services.import_export.archive_extractor import iter_archive

_SMALL_DIRECTORY = 64 * 1024


def _zip_of(junk: int, files: dict[str, bytes] | None = None) -> bytes:
    """A ZIP of *junk* empty unsupported entries, then *files*."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(junk):
            archive.writestr(f"{index:07d}.x", b"")
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _claiming_one_entry(data: bytes) -> bytes:
    """*data* with its end record saying it holds one entry; ``zipfile`` reads the directory by its size, not this."""
    at = data.rfind(b"PK\x05\x06")
    return data[: at + 8] + struct.pack("<HH", 1, 1) + data[at + 12 :]


def _tgz_of(junk: int, files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(junk):
            info = tarfile.TarInfo(f"{index:07d}.x")
            archive.addfile(info, io.BytesIO(b""))
        for name, content in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


_CSV = {"places.csv": b"name,latitude,longitude\nMill,42.1,-73.9\n"}


class ZipDirectoryTests(SimpleTestCase):
    def test_a_directory_past_the_bound_is_refused(self) -> None:
        with (
            mock.patch.object(archive_extractor, "MAX_ZIP_DIRECTORY_BYTES", _SMALL_DIRECTORY),
            self.assertRaises(ValueError),
        ):
            list(iter_archive(_zip_of(2_000, _CSV)))

    def test_an_end_record_claiming_one_entry_does_not_get_it_past(self) -> None:
        data = _claiming_one_entry(_zip_of(2_000, _CSV))
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(data)).infolist()), 2_001, "zipfile reads every entry anyway")

        with (
            mock.patch.object(archive_extractor, "MAX_ZIP_DIRECTORY_BYTES", _SMALL_DIRECTORY),
            self.assertRaises(ValueError),
        ):
            list(iter_archive(data))

    def test_a_directory_within_the_bound_is_read(self) -> None:
        with mock.patch.object(archive_extractor, "MAX_ZIP_DIRECTORY_BYTES", _SMALL_DIRECTORY):
            names = [entry.name for entry in iter_archive(_zip_of(50, _CSV))]

        self.assertEqual(names, ["places.csv"])

    def test_a_zip64_directory_within_the_bound_is_read(self) -> None:
        """Past 65,535 entries the end record points to a ZIP64 one, which ``zipfile`` reads through the same file."""
        names = [entry.name for entry in iter_archive(_zip_of(70_000, _CSV))]

        self.assertEqual(names, ["places.csv"])

    def test_refusing_a_large_directory_reads_little_of_it(self) -> None:
        data = _zip_of(200_000)
        self.assertGreater(len(data), 2 * archive_extractor.MAX_ZIP_DIRECTORY_BYTES)

        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                list(iter_archive(data))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertLess(peak, archive_extractor.MAX_ZIP_DIRECTORY_BYTES + 4 * 1024 * 1024)

    def test_opening_a_small_zip_holds_about_what_it_reads(self) -> None:
        """zipfile reads the end record to the file's end, and a buffered ``read(n)`` allocates ``n`` before reading.

        Asking a real file for the whole bound there costs 8 MiB per ZIP opened. A ``BytesIO`` slices instead, which is
        why this opens a file on disk. See UrbanLens#298 ("Nine tests fail under bin/host_pytest.sh on release/v_0_9_0").
        """
        with tempfile.TemporaryFile() as handle:
            handle.write(_zip_of(0, _CSV))
            handle.seek(0)
            tracemalloc.start()
            try:
                with archive_extractor.open_zip(handle) as archive:
                    names = archive.namelist()
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(names, ["places.csv"])
        self.assertLess(peak, 1024 * 1024, f"opening a one-entry ZIP peaked at {peak:,} bytes")


class TarMemberTests(SimpleTestCase):
    def test_members_past_the_bound_are_refused_supported_or_not(self) -> None:
        with mock.patch.object(archive_extractor, "MAX_TAR_MEMBERS", 100), self.assertRaises(ValueError):
            list(iter_archive(_tgz_of(150, _CSV)))

    def test_members_within_the_bound_are_read(self) -> None:
        with mock.patch.object(archive_extractor, "MAX_TAR_MEMBERS", 100):
            names = [entry.name for entry in iter_archive(_tgz_of(50, _CSV))]

        self.assertEqual(names, ["places.csv"])


class DataImportDirectoryTests(SimpleTestCase):
    """A backup restore opened the upload with ``zipfile`` and counted its members after reading them all."""

    def test_a_directory_past_the_bound_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            zip_path = os.path.join(workdir, "upload.zip")
            with open(zip_path, "wb") as handle:
                handle.write(_claiming_one_entry(_zip_of(2_000)))

            with (
                mock.patch.object(archive_extractor, "MAX_ZIP_DIRECTORY_BYTES", _SMALL_DIRECTORY),
                self.assertRaises(import_data._ImportValidationError) as refused,
            ):
                import_data._extract_and_validate(zip_path, os.path.join(workdir, "job"), job_id="test-job")

        self.assertIn("too many files", str(refused.exception))
