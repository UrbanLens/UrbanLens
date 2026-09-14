"""The upload's extraction budget bounds how much one entry reads, not only what is kept afterwards (P95).

Each entry used to be read up to the 1 GB per-file cap and charged to the budget only once it was in
memory, so an upload with 10 MB of allowance left could still pull a gigabyte into the sandbox worker
before the budget refused it.
"""

from __future__ import annotations

import io
import tarfile
from unittest import mock
import zipfile

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_export.archive_extractor import ExtractionBudget, extract_archive

_ALLOWANCE = 1_000
_ENTRY = b"x" * 200_000


def _zip(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.csv", payload)
    return buf.getvalue()


def _tgz(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="big.csv")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _ReadSizeRecorder:
    """Wraps an entry's file object and records every size it is asked to read."""

    def __init__(self, handle, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        return self._handle.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._handle.close()

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


class ZipReadIsBoundedByTheBudgetTests(SimpleTestCase):
    def test_an_entry_larger_than_the_remaining_allowance_is_refused_without_reading_it_all(self) -> None:
        sizes: list[int] = []
        original_open = zipfile.ZipFile.open

        def recording_open(self, *args, **kwargs):
            return _ReadSizeRecorder(original_open(self, *args, **kwargs), sizes)

        with mock.patch.object(zipfile.ZipFile, "open", recording_open), self.assertRaises(ValueError):
            extract_archive(_zip(_ENTRY), ExtractionBudget(max_bytes=_ALLOWANCE, max_files=10))

        self.assertTrue(sizes, "the premise failed: the entry was never read")
        self.assertTrue(
            all(0 <= size <= _ALLOWANCE + 1 for size in sizes), f"read sizes {sizes} exceeded the allowance"
        )

    def test_an_entry_within_the_allowance_still_extracts_whole(self) -> None:
        extracted = extract_archive(_zip(b"y" * 500), ExtractionBudget(max_bytes=_ALLOWANCE, max_files=10))

        self.assertEqual([entry.data for entry in extracted], [b"y" * 500])


class TgzReadIsBoundedByTheBudgetTests(SimpleTestCase):
    def test_a_member_larger_than_the_remaining_allowance_is_refused_without_reading_it_all(self) -> None:
        sizes: list[int] = []
        original_extractfile = tarfile.TarFile.extractfile

        def recording_extractfile(self, member):
            handle = original_extractfile(self, member)
            return None if handle is None else _ReadSizeRecorder(handle, sizes)

        with mock.patch.object(tarfile.TarFile, "extractfile", recording_extractfile), self.assertRaises(ValueError):
            extract_archive(_tgz(_ENTRY), ExtractionBudget(max_bytes=_ALLOWANCE, max_files=10))

        self.assertTrue(sizes, "the premise failed: the member was never read")
        self.assertTrue(
            all(0 <= size <= _ALLOWANCE + 1 for size in sizes), f"read sizes {sizes} exceeded the allowance"
        )

    def test_a_member_within_the_allowance_still_extracts_whole(self) -> None:
        extracted = extract_archive(_tgz(b"y" * 500), ExtractionBudget(max_bytes=_ALLOWANCE, max_files=10))

        self.assertEqual([entry.data for entry in extracted], [b"y" * 500])
