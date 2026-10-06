"""A data export copies photos and overlay images out of a storage that has no local paths (P321).

Production stores media with ``GatedS3Storage``, which, like every ``S3Storage``, has no ``path()``. The export asked
each file for its path, so one stored photo or image overlay failed the whole export.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from email.utils import formatdate
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from unittest.mock import patch
import zipfile

from botocore.awsrequest import AWSRequest, AWSResponse
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage, default_storage, storages
from django.test import override_settings
from django.utils._os import safe_join
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.services.import_export.export import run_export
from urbanlens.dashboard.services.media.object_storage import GatedS3Storage
from urbanlens.dashboard.tests.hypothesis.test_object_store_client_config import garage_unavailable, object_store

_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]
_STATICFILES = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
_PHOTO = b"\xff\xd8\xff\xe0 photo bytes " * 64
_OVERLAY = b"\x89PNG\r\n\x1a\n overlay bytes " * 64


class PathlessInMemoryStorage(InMemoryStorage):
    """Django's in-memory backend without ``path()``, which ``S3Storage`` does not have either.

    ``InMemoryStorage.path`` answers with a path nothing exists at, so the old export skipped the file silently there
    instead of failing as it does on S3; this backend raises the way S3 does.
    """

    def path(self, name: str) -> str:
        raise NotImplementedError("This backend doesn't support absolute paths.")

    def _relative_path(self, name: str) -> str:
        return os.path.relpath(safe_join(self.location, name), self.location)


_PATHLESS_STORAGES = {"default": {"BACKEND": f"{__name__}.PathlessInMemoryStorage"}, "staticfiles": _STATICFILES}


class _ExportCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _photo(self, name: str, content: bytes | None) -> Image:
        """An Image row naming *name*, with *content* stored there (None leaves storage without it)."""
        if content is not None:
            stored = default_storage.save(name, ContentFile(content))
            self.assertEqual(stored, name)
        return baker.make(Image, profile=self.profile, pin=self.pin, image=name)

    def _overlay(self, image: Image) -> MapImageOverlay:
        overlay = MapImageOverlay(image=image, name="Sanborn", parent_pin=self.pin, profile=self.profile)
        overlay.set_corners(_CORNERS)
        overlay.save()
        return overlay

    def _export(self) -> dict[str, bytes]:
        """Run the real export for photos and map annotations, returning the archive's members by relative path."""
        with (
            tempfile.TemporaryDirectory() as export_dir_path,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
        ):
            ok = run_export(
                self.user.pk, ["photos", "map_annotations"], export_dir_path, "https://example.test", job_id="p321"
            )
            self.assertTrue(ok, "run_export reported failure")
            with zipfile.ZipFile(Path(export_dir_path) / "export.zip") as zf:
                return {Path(*Path(name).parts[1:]).as_posix(): zf.read(name) for name in zf.namelist()}

    @staticmethod
    def _photo_rows(members: dict[str, bytes]) -> list[dict]:
        return json.loads(members["photos/metadata.json"])

    @staticmethod
    def _overlay_rows(members: dict[str, bytes]) -> list[dict]:
        return json.loads(members["map_annotations.json"])["overlays"]


class AnExportOverAStorageWithoutPathsTests(_ExportCase):
    """The storage API alone is enough: what ``S3Storage`` offers, and no more."""

    def setUp(self) -> None:
        super().setUp()
        overridden = override_settings(STORAGES=_PATHLESS_STORAGES)
        overridden.enable()
        self.addCleanup(overridden.disable)
        self.assertIsInstance(default_storage, PathlessInMemoryStorage)
        with self.assertRaises(NotImplementedError):
            default_storage.path("pin_images/any.jpg")

    def test_the_photo_and_the_overlay_bytes_land_in_the_archive(self) -> None:
        self._photo("pin_images/ab/cdef/2019-photo.jpg", _PHOTO)
        self._overlay(self._photo("pin_images/gh/ijkl/sanborn.png", _OVERLAY))

        members = self._export()

        self.assertEqual(members["photos/2019-photo.jpg"], _PHOTO)
        self.assertEqual(members["map_annotations/sanborn.png"], _OVERLAY)
        self.assertEqual({row["filename"] for row in self._photo_rows(members)}, {"2019-photo.jpg", "sanborn.png"})
        self.assertEqual([row["filename"] for row in self._overlay_rows(members)], ["sanborn.png"])

    def test_a_shared_basename_gets_the_rows_pk_and_neither_copy_is_lost(self) -> None:
        first = self._photo("pin_images/ab/one/IMG_0001.jpg", b"first")
        second = self._photo("pin_images/cd/two/IMG_0001.jpg", b"second")

        members = self._export()

        by_uuid = {row["uuid"]: row["filename"] for row in self._photo_rows(members)}
        self.assertEqual(by_uuid[str(first.uuid)], "IMG_0001.jpg")
        self.assertEqual(by_uuid[str(second.uuid)], f"IMG_0001_{second.pk}.jpg")
        self.assertEqual(members["photos/IMG_0001.jpg"], b"first")
        self.assertEqual(members[f"photos/IMG_0001_{second.pk}.jpg"], b"second")

    def test_a_file_missing_from_storage_still_exports_its_row_without_a_filename(self) -> None:
        photo = self._photo("pin_images/ab/gone/lost.jpg", None)
        overlay = self._overlay(self._photo("pin_images/cd/gone/lost-sheet.png", None))

        members = self._export()

        photo_row = next(row for row in self._photo_rows(members) if row["uuid"] == str(photo.uuid))
        self.assertIsNone(photo_row["filename"])
        overlay_row = next(row for row in self._overlay_rows(members) if row["uuid"] == str(overlay.uuid))
        self.assertIsNone(overlay_row["filename"])
        self.assertFalse([name for name in members if name.endswith((".jpg", ".png"))])


class AnExportOnLocalStorageKeepsTheFilesModifiedTimeTests(_ExportCase):
    """``shutil.copy2`` carried the stored file's modified time into the archive; streaming must too."""

    def test_the_archived_photo_is_dated_when_it_was_stored(self) -> None:
        image = self._photo("pin_images/ab/cdef/dated.jpg", _PHOTO)
        stored_at = 1_500_000_000
        os.utime(image.image.path, (stored_at, stored_at))

        with (
            tempfile.TemporaryDirectory() as export_dir_path,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
        ):
            self.assertTrue(
                run_export(self.user.pk, ["photos"], export_dir_path, "https://example.test", job_id="p321")
            )
            with zipfile.ZipFile(Path(export_dir_path) / "export.zip") as zf:
                info = next(info for info in zf.infolist() if info.filename.endswith("/photos/dated.jpg"))

        self.assertEqual(info.date_time, time.localtime(stored_at)[:6])


# -- The production backend itself, against a fake object store -------------------------------------------------------


class _Raw(io.BytesIO):
    """A response body botocore can both stream and read."""

    def stream(self, **_: object) -> Iterator[bytes]:
        yield self.getvalue()


_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


#: When the fake bucket says each of its objects was last modified.
_STORED_AT = 1_500_000_000


def _key(request: AWSRequest) -> str:
    return request.url.split("?", 1)[0].split("/ul-media/", 1)[-1]


def _bucket(
    objects: dict[str, bytes], *, deleted_after_head: frozenset[str] = frozenset()
) -> Callable[[AWSRequest], AWSResponse]:
    """Answer HEAD and GET (whole or ranged) for *objects*, keyed by object name; anything else is a 404.

    Args:
        objects: The bucket's contents.
        deleted_after_head: Keys whose HEAD finds them but whose GET does not, as when a file is deleted mid-export.
    """
    modified = formatdate(_STORED_AT, usegmt=True)

    def respond(request: AWSRequest) -> AWSResponse:
        key = _key(request)
        data = objects.get(key)
        if data is None or (request.method == "GET" and key in deleted_after_head):
            return AWSResponse(request.url, 404, {"Content-Length": "0"}, _Raw(b""))
        headers = {"Last-Modified": modified, "ETag": '"etag"', "Content-Type": "application/octet-stream"}
        if request.method == "HEAD":
            return AWSResponse(request.url, 200, {**headers, "Content-Length": str(len(data))}, _Raw(b""))
        status, body = 200, data
        requested = request.headers.get("Range") or ""
        if match := _RANGE.fullmatch(requested.decode() if isinstance(requested, bytes) else requested):
            start, end = int(match[1]), min(int(match[2] or len(data) - 1), len(data) - 1)
            status, body = 206, data[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return AWSResponse(request.url, status, {**headers, "Content-Length": str(len(body))}, _Raw(body))

    return respond


class AnExportOverTheProductionObjectStoreTests(_ExportCase):
    """``GatedS3Storage`` built from production's options, answering from an in-process bucket."""

    def test_the_photo_and_the_overlay_bytes_land_in_the_archive(self) -> None:
        objects = {"pin_images/ab/cdef/2019-photo.jpg": _PHOTO, "pin_images/gh/ijkl/sanborn.png": _OVERLAY}
        with object_store(_bucket(objects)):
            baker.make(Image, profile=self.profile, pin=self.pin, image="pin_images/ab/cdef/2019-photo.jpg")
            self._overlay(baker.make(Image, profile=self.profile, pin=self.pin, image="pin_images/gh/ijkl/sanborn.png"))
            members = self._export()

        self.assertEqual(members["photos/2019-photo.jpg"], _PHOTO)
        self.assertEqual(members["map_annotations/sanborn.png"], _OVERLAY)

    def test_an_object_the_store_does_not_have_is_exported_without_a_filename(self) -> None:
        with object_store(_bucket({})):
            photo = baker.make(Image, profile=self.profile, pin=self.pin, image="pin_images/ab/gone/lost.jpg")
            members = self._export()

        self.assertIsNone(self._photo_rows(members)[0]["filename"])
        self.assertEqual(self._photo_rows(members)[0]["uuid"], str(photo.uuid))

    def test_an_object_deleted_after_it_was_found_is_exported_without_a_filename(self) -> None:
        """S3Storage turns a 404 into FileNotFoundError when it opens a file, but the download comes later."""
        name = "pin_images/ab/race/deleted.jpg"
        with object_store(_bucket({name: _PHOTO}, deleted_after_head=frozenset({name}))):
            baker.make(Image, profile=self.profile, pin=self.pin, image=name)
            members = self._export()

        self.assertIsNone(self._photo_rows(members)[0]["filename"])
        self.assertNotIn("photos/deleted.jpg", members)

    def test_a_store_that_fails_fails_the_export_rather_than_leaving_the_file_out(self) -> None:
        def forbidden(request: AWSRequest) -> AWSResponse:
            return AWSResponse(request.url, 403, {"Content-Length": "0"}, _Raw(b""))

        name = "pin_images/ab/down/photo.jpg"
        baker.make(Image, profile=self.profile, pin=self.pin, image=name)
        for respond in (garage_unavailable, forbidden):
            with (
                self.subTest(respond=respond.__name__),
                object_store(respond),
                tempfile.TemporaryDirectory() as export_dir_path,
                patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
            ):
                ok = run_export(self.user.pk, ["photos"], export_dir_path, "https://example.test", job_id="p321")
                self.assertFalse(ok)
                self.assertFalse((Path(export_dir_path) / "export.zip").exists())

    def test_the_archived_photo_is_dated_when_the_store_says_it_was_stored(self) -> None:
        name = "pin_images/ab/cdef/dated.jpg"
        with (
            object_store(_bucket({name: _PHOTO})),
            tempfile.TemporaryDirectory() as export_dir_path,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
        ):
            baker.make(Image, profile=self.profile, pin=self.pin, image=name)
            self.assertTrue(
                run_export(self.user.pk, ["photos"], export_dir_path, "https://example.test", job_id="p321")
            )
            with zipfile.ZipFile(Path(export_dir_path) / "export.zip") as zf:
                info = next(info for info in zf.infolist() if info.filename.endswith("/photos/dated.jpg"))

        self.assertEqual(info.date_time, time.localtime(_STORED_AT)[:6])

    def test_the_export_asks_the_store_nothing_beyond_what_reading_the_object_takes(self) -> None:
        name = "pin_images/ab/cdef/counted.jpg"
        with object_store(_bucket({name: _PHOTO})) as sent, default_storage.open(name, "rb") as source:
            source.read()
        reading = [request.method for request in sent if _key(request) == name]

        with object_store(_bucket({name: _PHOTO})) as sent:
            baker.make(Image, profile=self.profile, pin=self.pin, image=name)
            self._export()
        exporting = [request.method for request in sent if _key(request) == name]

        self.assertEqual(exporting, reading)


class AReadFromTheProductionObjectStoreIsBoundedTests(SimpleTestCase):
    """``S3File`` downloads the whole object before the first read; past a size, it must spool that to disk, not RAM."""

    def test_a_large_object_spools_to_disk_and_copies_intact(self) -> None:
        with object_store(_bucket({})):
            storage = storages["default"]
            self.assertIsInstance(storage, GatedS3Storage)
            threshold = getattr(storage, "max_memory_size", 0)
        self.assertGreater(threshold, 0, "S3File keeps every object it opens wholly in memory")

        data = bytes(range(256)) * ((threshold // 256) + 1)
        with object_store(_bucket({"pin_images/ab/big/video.mp4": data})), tempfile.TemporaryFile() as out:
            with default_storage.open("pin_images/ab/big/video.mp4", "rb") as source:
                shutil.copyfileobj(source, out)
                self.assertTrue(getattr(source.file, "_rolled", False), "the downloaded object stayed in memory")
            out.seek(0)
            self.assertEqual(out.read(), data)
