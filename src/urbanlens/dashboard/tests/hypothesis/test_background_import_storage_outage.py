"""A background photo import waits out an object store that refuses a write, then imports the rest (P220).

The Immich, Flickr, Flickr album and Google Photos imports, and an export archive's photos and overlay images, stored
one file at a time and caught only a refused reservation. A Garage 503 or timeout on one photo failed the whole task
(or the archive import), and the rest of the selection was never imported. Storage here is the real S3 backend, built
from production's options and answered in-process by a fake Garage, so nothing leaves the process.
"""

from __future__ import annotations

from collections.abc import Callable
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, ClassVar
from unittest import mock
import uuid
import zipfile

from botocore.awsrequest import AWSRequest, AWSResponse
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway
from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbumPhoto, FlickrPublicGateway
from urbanlens.dashboard.services.apis.immich import ImmichGateway
from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway, session_items_cache_key
from urbanlens.dashboard.services.core.celery import RetryNoticeError, TaskProgress, get_task_progress
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus
from urbanlens.dashboard.tests.hypothesis.test_object_store_client_config import (
    _Body,
    garage_stalled,
    garage_unavailable,
    object_store,
)

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"

#: The waits a selection makes while storage stays down: 1, 2, 4, 8 and 15 minutes.
_FULL_BACKOFF = [60, 120, 240, 480, 900]


def garage_stores(request: AWSRequest) -> AWSResponse:
    """What Garage answers a write it stored."""
    return AWSResponse(request.url, 200, {"ETag": '"stored"', "Content-Length": "0"}, _Body(b""))


class _Garage:
    """An object store that stores every write, and refuses each one while it is down.

    Attributes:
        down: Whether writes are refused.
        down_after: Go down once this many writes have been stored, or never when None.
        refusal: How a write is refused while down.
        stored: Each stored write's URL.
        refused: How many write requests were refused, botocore's own retries included.
    """

    def __init__(
        self, *, down_after: int | None = None, refusal: Callable[[AWSRequest], AWSResponse] = garage_unavailable
    ) -> None:
        self.down = False
        self.down_after = down_after
        self.refusal = refusal
        self.stored: list[str] = []
        self.refused = 0

    def reset(self, *, down_after: int | None, refusal: Callable[[AWSRequest], AWSResponse]) -> None:
        """Start over, up, with nothing stored."""
        self.down, self.down_after, self.refusal = False, down_after, refusal
        self.stored.clear()
        self.refused = 0

    def __call__(self, request: AWSRequest) -> AWSResponse:
        if request.method != "PUT":
            return AWSResponse(request.url, 404, {"Content-Length": "0"}, _Body(b""))
        if self.down_after is not None and len(self.stored) >= self.down_after:
            self.down, self.down_after = True, None
        if self.down:
            self.refused += 1
            return self.refusal(request)
        self.stored.append(request.url)
        return garage_stores(request)


def _jpeg(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (24, 16), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class _StorageCase(TestCase):
    """Media on a temporary directory, every enqueue stubbed, and a fake Garage behind default storage."""

    task: ClassVar[Any]

    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_import_outage_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.media_root = media_root
        self.enterContext(override_settings(MEDIA_ROOT=media_root))
        self.enqueue = self.enterContext(mock.patch(_ENQUEUE))
        self.garage = _Garage()
        self.enterContext(object_store(self.garage))
        self.retries: list[dict[str, Any]] = []
        #: Run as the task asks to retry, before the retry runs.
        self.on_retry: Callable[[], None] = lambda: None
        original = self.task.retry

        def retry(*args: Any, **kwargs: Any) -> Retry:
            self.retries.append(kwargs)
            self.on_retry()
            return original(*args, **kwargs)

        self.enterContext(mock.patch.object(self.task, "retry", side_effect=retry))
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def storage_comes_back_on_retry(self) -> None:
        def recover() -> None:
            self.garage.down = False

        self.on_retry = recover

    @property
    def countdowns(self) -> list[int]:
        return [kwargs["countdown"] for kwargs in self.retries]


class _LibraryImportTests(_StorageCase):
    """What every library and album import must do; each subclass runs it against one task."""

    __test__ = False
    photo_ids: ClassVar[list[str]] = ["p1", "p2", "p3"]

    def setUp(self) -> None:
        super().setUp()
        self.downloaded: list[str] = []
        self.content = {photo_id: _jpeg((10 * n, 20, 30)) for n, photo_id in enumerate(self.photo_ids, start=1)}

    def run_import(self, photo_ids: list[str]) -> dict[str, int]:
        """Run the task eagerly, retries included, and return its counts."""
        raise NotImplementedError

    def imported_rows(self) -> Any:
        return Image.objects.filter(profile=self.profile)

    def test_a_write_garage_refuses_is_retried_and_the_rest_of_the_selection_imported(self) -> None:
        for refusal in (garage_unavailable, garage_stalled):
            with self.subTest(refusal=refusal.__name__):
                self.garage.reset(down_after=1, refusal=refusal)
                self.storage_comes_back_on_retry()
                self.downloaded.clear()
                self.retries.clear()
                self.imported_rows().delete()

                counts = self.run_import(self.photo_ids)

                self.assertEqual(counts["imported"], 3, counts)
                self.assertEqual(counts["storage_unavailable"], 0, counts)
                self.assertEqual(self.imported_rows().count(), 3)
                self.assertEqual(len(set(self.imported_rows().values_list("checksum", flat=True))), 3)
                self.assertTrue(all(self.imported_rows().values_list("pending_scan", flat=True)))
                self.assertEqual(self.countdowns, [60])
                # The stored photo is not downloaded again; only the one storage refused is.
                self.assertEqual(self.downloaded, ["p1", "p2", "p2", "p3"])

    def test_storage_that_stays_down_backs_off_then_counts_the_rest_as_not_imported(self) -> None:
        self.garage.down_after = 1

        counts = self.run_import(self.photo_ids)

        self.assertEqual(self.countdowns, _FULL_BACKOFF)
        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0, "storage_unavailable": 2})
        self.assertEqual(self.imported_rows().count(), 1)
        # One write is tried per wait, not one per photo left.
        self.assertEqual(self.downloaded, ["p1", *["p2"] * (1 + len(_FULL_BACKOFF))])


class ImmichImportStorageTests(_LibraryImportTests):
    __test__ = True
    task = tasks.import_immich_photos

    def setUp(self) -> None:
        super().setUp()
        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def run_import(self, photo_ids: list[str]) -> dict[str, int]:
        def download(_gateway: ImmichGateway, asset_id: str) -> tuple[bytes, str, str]:
            self.downloaded.append(asset_id)
            return self.content[asset_id], f"{asset_id}.jpg", "image/jpeg"

        with mock.patch.object(ImmichGateway, "get_asset_original", download):
            return self.task.apply(args=(self.pin.pk, self.profile.pk, photo_ids)).get()

    def test_the_wait_tells_the_poller_storage_is_unavailable_and_how_far_it_got(self) -> None:
        self.garage.down_after = 1
        self.storage_comes_back_on_retry()

        self.run_import(self.photo_ids)

        notice = self.retries[0]["exc"]
        self.assertIsInstance(notice, RetryNoticeError)
        self.assertIn("storage", notice.message.lower())
        self.assertEqual((notice.current, notice.total), (1, 3))

    def test_a_refusal_after_progress_starts_the_backoff_again(self) -> None:
        """Two separate outages each wait a minute first; only refusals with nothing stored between them back off."""
        self.garage.down_after = 1

        def recover_then_fail_again() -> None:
            self.garage.down = False
            if len(self.retries) == 1:
                self.garage.down_after = 2

        self.on_retry = recover_then_fail_again

        counts = self.run_import(self.photo_ids)

        self.assertEqual(counts["imported"], 3, counts)
        self.assertEqual(self.countdowns, [60, 60])

    def test_a_visit_target_survives_the_retry(self) -> None:
        visit = baker.make_recipe("dashboard.pin_visit", pin=self.pin)
        self.garage.down_after = 0
        self.storage_comes_back_on_retry()

        def download(_gateway: ImmichGateway, asset_id: str) -> tuple[bytes, str, str]:
            return self.content[asset_id], f"{asset_id}.jpg", "image/jpeg"

        with mock.patch.object(ImmichGateway, "get_asset_original", download):
            self.task.apply(args=(self.pin.pk, self.profile.pk, ["p1"], {"p1": visit.pk})).get()

        self.assertEqual(Image.objects.get(profile=self.profile).visit_id, visit.pk)

    def test_a_download_that_fails_outside_storage_is_not_taken_for_storage(self) -> None:
        def download(_gateway: ImmichGateway, asset_id: str) -> tuple[bytes, str, str]:
            raise ConnectionError("Immich is unreachable")

        with (
            mock.patch.object(ImmichGateway, "get_asset_original", download),
            self.assertRaises(ConnectionError),
        ):
            self.task.apply(args=(self.pin.pk, self.profile.pk, ["p1"])).get()

        self.assertFalse(any(isinstance(kwargs.get("exc"), RetryNoticeError) for kwargs in self.retries))


class FlickrImportStorageTests(_LibraryImportTests):
    __test__ = True
    task = tasks.import_flickr_photos

    def setUp(self) -> None:
        super().setUp()
        FlickrAccount.objects.create(
            profile=self.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00"
        )

    def run_import(self, photo_ids: list[str]) -> dict[str, int]:
        def download(_gateway: FlickrGateway, photo_id: str) -> tuple[bytes, str, str]:
            self.downloaded.append(photo_id)
            return self.content[photo_id], f"{photo_id}.jpg", "image/jpeg"

        with mock.patch.object(FlickrGateway, "get_original", download):
            return self.task.apply(args=(self.pin.pk, self.profile.pk, photo_ids)).get()


class FlickrAlbumImportStorageTests(_LibraryImportTests):
    __test__ = True
    task = tasks.import_flickr_album_photos
    album_url = "https://www.flickr.com/photos/12345678@N00/albums/1"

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=self.pin.location)

    def imported_rows(self) -> Any:
        return Image.objects.filter(profile=self.profile, wiki=self.wiki)

    def run_import(self, photo_ids: list[str]) -> dict[str, int]:
        photos = [
            FlickrAlbumPhoto(
                id=photo_id,
                title=photo_id,
                thumbnail_url=None,
                download_url=f"https://example.com/{photo_id}.jpg",
                author=None,
                taken_at=None,
            )
            for photo_id in self.photo_ids
        ]
        album = mock.MagicMock(owner_nsid="12345678@N00", photos=photos)

        def download(_gateway: FlickrPublicGateway, photo: FlickrAlbumPhoto) -> tuple[bytes, str, str]:
            self.downloaded.append(photo.id)
            return self.content[photo.id], f"{photo.id}.jpg", "image/jpeg"

        with (
            mock.patch.object(FlickrPublicGateway, "get_album", return_value=album),
            mock.patch.object(FlickrPublicGateway, "download_photo", download),
        ):
            return self.task.apply(args=("wiki", self.wiki.pk, self.profile.pk, self.album_url, photo_ids)).get()


class GooglePhotosImportStorageTests(_LibraryImportTests):
    __test__ = True
    task = tasks.import_google_photos

    def setUp(self) -> None:
        super().setUp()
        GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")
        cache.set(
            session_items_cache_key("sess"),
            {
                photo_id: {
                    "base_url": f"https://x/{photo_id}",
                    "mime_type": "image/jpeg",
                    "filename": f"{photo_id}.jpg",
                }
                for photo_id in self.photo_ids
            },
            3600,
        )

    def run_import(self, photo_ids: list[str]) -> dict[str, int]:
        def download(_gateway: GooglePhotosGateway, base_url: str, *, original: bool = True) -> bytes:
            photo_id = base_url.rsplit("/", 1)[-1]
            self.downloaded.append(photo_id)
            return self.content[photo_id]

        with mock.patch.object(GooglePhotosGateway, "download_media_item", download):
            return self.task.apply(args=(self.pin.pk, self.profile.pk, "sess", photo_ids)).get()


class RetryNoticeProgressTests(TestCase):
    def test_a_task_waiting_to_retry_reports_its_notice_to_the_poller(self) -> None:
        task_id = str(uuid.uuid4())
        tasks.import_immich_photos.backend.mark_as_retry(
            task_id, RetryNoticeError("Storage is briefly unavailable.", 2, 8)
        )

        progress = get_task_progress(task_id)

        self.assertEqual(progress.state, "RETRY")
        self.assertEqual(progress.message, "Storage is briefly unavailable.")
        self.assertEqual((progress.current, progress.total, progress.percent), (2, 8, 25))

    def test_any_other_retry_reason_is_not_shown(self) -> None:
        """A raw exception is for the logs; the poller gets no message from it."""
        task_id = str(uuid.uuid4())
        tasks.import_immich_photos.backend.mark_as_retry(task_id, OSError("ReadTimeoutError: http://objectstore:3900"))

        self.assertEqual(get_task_progress(task_id).message, "")


class ImportProgressViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        user = baker.make(User)
        self.client.force_login(user)
        self.pin = baker.make(Pin, profile=user.profile, location=baker.make(Location))

    def _poll(self, progress: TaskProgress) -> Any:
        with mock.patch("urbanlens.dashboard.controllers.immich.get_task_progress", return_value=progress):
            return self.client.get(reverse("pin.immich.import.progress", args=[self.pin.slug, "task-1"]))

    def test_a_waiting_import_shows_the_storage_notice_and_keeps_polling(self) -> None:
        message = "Storage is briefly unavailable. Imported 1 of 3 so far; trying the rest again in 1 minute(s)..."
        response = self._poll(
            TaskProgress(task_id="task-1", state="RETRY", current=1, total=3, percent=33, message=message)
        )

        self.assertContains(response, message)
        self.assertContains(response, "hx-get")

    def test_photos_left_for_storage_end_in_a_warning_toast_that_says_so(self) -> None:
        result = {"imported": 1, "skipped": 0, "failed": 0, "storage_unavailable": 2}
        response = self._poll(TaskProgress(task_id="task-1", state="SUCCESS", result=result))

        toast = json.loads(response["HX-Trigger"])["showToast"]
        self.assertEqual(toast["level"], "warning")
        self.assertIn("2 not imported because storage was unavailable", toast["message"])

    def test_a_clean_import_toasts_success(self) -> None:
        result = {"imported": 3, "skipped": 0, "failed": 0, "storage_unavailable": 0}
        response = self._poll(TaskProgress(task_id="task-1", state="SUCCESS", result=result))

        self.assertEqual(
            json.loads(response["HX-Trigger"])["showToast"], {"level": "success", "message": "Imported 3 photo(s)."}
        )


class _ArchiveCase(_StorageCase):
    task = tasks.run_user_data_import

    def setUp(self) -> None:
        super().setUp()
        self.job_id = str(uuid.uuid4())
        self.job_dir = Path(self.media_root) / "imports" / self.job_id
        self.job_dir.mkdir(parents=True)
        self.zip_path = str(self.job_dir / "upload.zip")
        self.photo_uuids = [str(uuid.uuid4()) for _ in range(3)]
        self.statuses: list[dict[str, Any]] = []
        original_write = ImportJobStatus.write
        statuses = self.statuses

        def write(status_self: ImportJobStatus, status: str, progress: int, message: str, **kwargs: Any) -> None:
            statuses.append({"status": status, "message": message})
            original_write(status_self, status, progress, message, **kwargs)

        self.enterContext(mock.patch.object(ImportJobStatus, "write", write))

    def write_archive(self, *, overlay: bool = False) -> None:
        root = "urbanlens_export_2026-10-04"
        contents = ["pins", "photos", "saved_filters"] + (["map_annotations"] if overlay else [])
        pin_uuid = str(self.pin.uuid)
        entries: dict[str, bytes] = {
            f"{root}/manifest.json": json.dumps({"format": "urbanlens_v1", "contents": contents}).encode(),
            f"{root}/pins.json": json.dumps([]).encode(),
            f"{root}/saved_filters.json": json.dumps([{"uuid": str(uuid.uuid4()), "name": "Rooftops"}]).encode(),
            f"{root}/photos/metadata.json": json.dumps(
                [
                    {"uuid": photo_uuid, "filename": f"photo{n}.jpg", "target_type": "pin", "target_uuid": pin_uuid}
                    for n, photo_uuid in enumerate(self.photo_uuids)
                ]
            ).encode(),
            **{f"{root}/photos/photo{n}.jpg": _jpeg((40 * n, 50, 60)) for n in range(len(self.photo_uuids))},
        }
        if overlay:
            corners = [[42.0, -73.0], [42.0, -72.99], [41.99, -72.99], [41.99, -73.0]]
            entries[f"{root}/map_annotations.json"] = json.dumps(
                {
                    "overlays": [
                        {
                            "uuid": str(uuid.uuid4()),
                            "filename": "overlay.jpg",
                            "corners": corners,
                            "target_type": "pin",
                            "target_uuid": pin_uuid,
                        }
                    ]
                }
            ).encode()
            entries[f"{root}/map_annotations/overlay.jpg"] = _jpeg((1, 2, 3))
        with zipfile.ZipFile(self.zip_path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def run_job(self) -> bool:
        ImportJobStatus(self.job_id).write("pending", 0, "Preparing import...", user_id=self.profile.user.pk)
        with self.captureOnCommitCallbacks(execute=True):
            return self.task.apply(args=(self.profile.user.pk, self.zip_path, self.job_id)).get()

    def final_status(self) -> dict[str, Any]:
        return ImportJobStatus(self.job_id).read()

    def cleanups_scheduled(self) -> int:
        return sum(1 for call in self.enqueue.call_args_list if call.args[0] is tasks.cleanup_import_artifacts_task)


class ArchivePhotoStorageTests(_ArchiveCase):
    def test_photos_storage_refused_are_stored_on_retry_and_none_twice(self) -> None:
        self.write_archive()
        self.garage.down_after = 1
        self.storage_comes_back_on_retry()

        self.assertTrue(self.run_job())

        rows = Image.objects.filter(profile=self.profile)
        self.assertEqual(sorted(str(value) for value in rows.values_list("uuid", flat=True)), sorted(self.photo_uuids))
        self.assertEqual(set(rows.values_list("pin_id", flat=True)), {self.pin.pk})
        self.assertEqual(self.countdowns, [60])
        status = self.final_status()
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["result"]["created"].get("photos"), 3)
        self.assertEqual(status["result"]["created"].get("saved_filters"), 1)
        self.assertNotIn("photos", status["result"]["skipped"])
        self.assertEqual(status["result"]["warnings"], [])
        self.assertEqual(self.cleanups_scheduled(), 1)

    def test_the_rest_of_the_archive_is_imported_while_the_photos_wait(self) -> None:
        """Steps after the photos do not wait for storage; only the files do."""
        self.write_archive()
        self.garage.down_after = 1
        saved_filter_at_retry: list[bool] = []

        def check() -> None:
            saved_filter_at_retry.append(SavedFilter.objects.filter(profile=self.profile, name="Rooftops").exists())
            self.garage.down = False

        self.on_retry = check

        self.run_job()

        self.assertEqual(saved_filter_at_retry, [True])
        self.assertEqual(SavedFilter.objects.filter(profile=self.profile, name="Rooftops").count(), 1)

    def test_the_wait_says_storage_is_unavailable(self) -> None:
        self.write_archive()
        self.garage.down_after = 1
        seen: list[dict[str, Any]] = []

        def look() -> None:
            seen.append(self.final_status())
            self.garage.down = False

        self.on_retry = look

        self.run_job()

        self.assertEqual(seen[0]["status"], "running")
        self.assertIn("storage", seen[0]["message"].lower())

    def test_storage_that_stays_down_finishes_the_import_and_says_what_was_left(self) -> None:
        self.write_archive()
        self.garage.down_after = 1

        self.assertTrue(self.run_job())

        self.assertEqual(self.countdowns, _FULL_BACKOFF)
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 1)
        status = self.final_status()
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["result"]["created"].get("photos"), 1)
        self.assertEqual(status["result"]["created"].get("saved_filters"), 1)
        self.assertTrue(
            any("storage" in warning.lower() and "2" in warning for warning in status["result"]["warnings"])
        )
        self.assertEqual(self.cleanups_scheduled(), 1)
        # One write is tried per run, not one per photo left.
        self.assertLessEqual(self.garage.refused, 2 * (1 + len(_FULL_BACKOFF)))

    def test_an_overlay_image_storage_refused_is_restored_on_retry(self) -> None:
        self.write_archive(overlay=True)
        self.garage.down_after = 3
        self.storage_comes_back_on_retry()

        self.run_job()

        overlay = MapImageOverlay.objects.get(profile=self.profile)
        self.assertIsNotNone(overlay.image_id)
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 4)
        self.assertEqual(self.countdowns, [60])
        status = self.final_status()
        self.assertEqual(status["result"]["created"].get("map_overlays"), 1)
        self.assertNotIn("map_overlays", status["result"]["skipped"])

    def test_a_pin_deleted_during_the_wait_leaves_its_photos_unattached_rather_than_failing(self) -> None:
        self.write_archive()
        self.garage.down_after = 1

        def delete_pin_and_recover() -> None:
            Pin.objects.filter(pk=self.pin.pk).delete()
            self.garage.down = False

        self.on_retry = delete_pin_and_recover

        self.assertTrue(self.run_job())

        self.assertEqual(self.final_status()["status"], "done")
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 3)


class ArchiveStepStorageTests(_ArchiveCase):
    def test_a_photo_storage_refuses_does_not_end_the_step(self) -> None:
        self.write_archive()
        data_dir = Path(self.media_root) / "extracted"
        with zipfile.ZipFile(self.zip_path) as archive:
            archive.extractall(data_dir)
        self.garage.down_after = 1
        result = import_data.ImportResult()

        import_data._import_photos(
            self.profile,
            str(data_dir / "urbanlens_export_2026-10-04"),
            result,
            pin_uuid_map={str(self.pin.uuid): self.pin.pk},
            label_uuid_map={},
        )

        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(result.created.get("photos"), 1)
        self.assertEqual(result.deferred.rows, {"photos": [2, 3]})

    def test_an_object_store_failure_that_escapes_a_step_is_reported_as_storage(self) -> None:
        """Nothing else stores a file today; a step that one day does must not read as a bad archive."""
        from botocore.exceptions import ClientError

        self.write_archive()
        refusal = ClientError(
            {"Error": {"Code": "ServiceUnavailable"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "PutObject"
        )
        with mock.patch.dict(import_data._IMPORTERS, {"saved_filters": mock.Mock(side_effect=refusal)}):
            self.assertFalse(self.run_job())

        status = self.final_status()
        self.assertEqual(status["status"], "error")
        self.assertIn("storage", status["message"].lower())
        self.assertNotIn("check the file", status["message"].lower())
