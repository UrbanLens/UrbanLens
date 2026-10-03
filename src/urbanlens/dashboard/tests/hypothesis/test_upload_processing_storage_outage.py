"""Processing an upload through a Garage outage waits for storage; it never removes the upload or marks it unreadable (P201).

The re-encode, the scan and the derived copies read and write media storage. A timeout from the object store is also an
OSError, which these paths took for an undecodable or unreadable file: a pending upload was retried three times over
about seven minutes and then removed, and a photo whose thumbnail write timed out was skipped by the backfill for a week.
"""

from __future__ import annotations

from datetime import timedelta
import io
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from botocore.exceptions import ClientError, ReadTimeoutError
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.upload_retry import UploadRetry
from urbanlens.dashboard.services.media import upload_retry
from urbanlens.dashboard.services.security.malware_scan import MalwareScanUnavailableError
from urbanlens.dashboard.tests.hypothesis.test_object_store_client_config import garage_unavailable, object_store

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_DOWNSCALE = "urbanlens.dashboard.services.media.images.downscale_stored_image"
_SCAN = "urbanlens.dashboard.services.security.malware_scan.malware_error_for_upload"
_THUMBNAIL = "urbanlens.dashboard.services.media.images.write_image_thumbnail"


def _timeout() -> ReadTimeoutError:
    """What botocore raises when Garage holds a request open past the read timeout; also an OSError."""
    return ReadTimeoutError(endpoint_url="http://objectstore:3900/ul-media/pin_images/x.jpg")


def _garage_503() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ServiceUnavailable"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "GetObject"
    )


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="ul_processing_outage_")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.media_root, MEDIA_X_ACCEL=False)
        media.enable()
        self.addCleanup(media.disable)
        cache.clear()
        enqueue = mock.patch(_ENQUEUE)
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        self.profile = baker.make(User).profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def _pending(self, name: str = "pin_images/raw.jpg", media_type: str = MediaKind.PHOTO) -> Image:
        target = Path(self.media_root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        PILImage.new("RGB", (32, 24), (40, 80, 120)).save(buffer, format="JPEG")
        target.write_bytes(buffer.getvalue())
        return baker.make(
            Image, profile=self.profile, pin=self.pin, image=name, pending_scan=True, media_type=media_type
        )

    def assert_waiting_for_storage(self, image: Image) -> None:
        row = Image.objects.filter(pk=image.pk).first()
        self.assertIsNotNone(row, "a storage outage removed the upload")
        assert row is not None
        self.assertTrue(row.pending_scan, "an upload that was never re-encoded was published")
        self.assertIsNone(row.upload_failed_at)
        self.assertTrue(UploadRetry.objects.filter(target=upload_retry.IMAGE, object_id=image.pk).exists())
        self.assertFalse(PhotoUploadFailure.objects.filter(profile=self.profile).exists())
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.profile, notification_type=NotificationType.PHOTO_UPLOAD_FAILED
            ).exists()
        )


class TheReencodeTests(_Case):
    def test_a_storage_timeout_on_every_retry_leaves_the_upload_waiting(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_timeout()) as downscale:
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assertEqual(downscale.call_count, tasks.process_image_upload.max_retries + 1)
        self.assert_waiting_for_storage(image)

    def test_a_refusal_from_garage_leaves_the_upload_waiting(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_garage_503()):
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assert_waiting_for_storage(image)

    def test_a_photo_that_cannot_be_decoded_is_still_removed(self) -> None:
        """The control: only storage waits. An undecodable upload is never published as uploaded."""
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=OSError("cannot identify image file")):
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_the_retry_sweep_queues_it_again_and_success_ends_the_wait(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_timeout()):
            tasks.process_image_upload.apply(args=(image.pk,))
        UploadRetry.objects.filter(target=upload_retry.IMAGE).update(
            next_attempt_at=timezone.now() - timedelta(minutes=1)
        )

        self.assertEqual(upload_retry.retry_waiting_uploads(), 1)
        queued = [call.args for call in self.enqueue.call_args_list if call.args[:1] == (tasks.process_image_upload,)]
        self.assertEqual(queued, [(tasks.process_image_upload, image.pk, None)])

        self.assertTrue(tasks.process_image_upload.apply(args=(image.pk,)).get())
        self.assertFalse(Image.objects.get(pk=image.pk).pending_scan)
        self.assertFalse(UploadRetry.objects.filter(target=upload_retry.IMAGE, object_id=image.pk).exists())

    def test_giving_up_offers_the_upload_back_to_its_owner_rather_than_removing_it(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_timeout()):
            tasks.process_image_upload.apply(args=(image.pk,))
        upload_retry.give_up(UploadRetry.objects.get(target=upload_retry.IMAGE, object_id=image.pk))
        self.assertTrue(Image.objects.filter(pk=image.pk, pending_scan=True).exists())
        self.assertTrue(PhotoUploadFailure.objects.filter(image=image).exists())

    def test_giving_up_on_a_photo_published_meanwhile_offers_nothing_back(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_timeout()):
            tasks.process_image_upload.apply(args=(image.pk,))
        Image.objects.filter(pk=image.pk).update(pending_scan=False)

        upload_retry.give_up(UploadRetry.objects.get(target=upload_retry.IMAGE, object_id=image.pk))

        self.assertIsNone(Image.objects.get(pk=image.pk).upload_failed_at)
        self.assertFalse(PhotoUploadFailure.objects.filter(image=image).exists())
        self.assertFalse(UploadRetry.objects.filter(target=upload_retry.IMAGE, object_id=image.pk).exists())

    def test_the_stall_sweep_leaves_an_upload_waiting_for_storage_to_the_retry_sweep(self) -> None:
        image = self._pending()
        with mock.patch(_DOWNSCALE, side_effect=_timeout()):
            tasks.process_image_upload.apply(args=(image.pk,))
        Image.objects.filter(pk=image.pk).update(created=timezone.now() - tasks.STALLED_UPLOAD_AGE * 2)
        self.enqueue.reset_mock()

        tasks.requeue_stalled_pending_uploads()

        self.enqueue.assert_not_called()
        self.assertEqual(Image.objects.get(pk=image.pk).upload_sweep_attempts, 0)


class TheScanTests(_Case):
    def test_garage_failing_the_scans_read_is_not_taken_for_the_scanner_being_down(self) -> None:
        """The scanner reports a failed read of its stream as itself being unavailable, and that rejects the upload."""
        image = self._pending()
        timeout = _timeout()
        unavailable = MalwareScanUnavailableError(str(timeout))
        unavailable.__cause__ = timeout
        with mock.patch(_SCAN, side_effect=unavailable):
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assert_waiting_for_storage(image)

    def test_the_scanner_being_down_still_rejects_once_its_retries_run_out(self) -> None:
        image = self._pending()
        with mock.patch(_SCAN, side_effect=MalwareScanUnavailableError("clamd down")):
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_garage_refusing_to_open_the_upload_leaves_it_waiting(self) -> None:
        image = self._pending()
        with object_store(garage_unavailable):
            tasks.process_image_upload.apply(args=(image.pk,))
        self.assert_waiting_for_storage(image)


class TheBackfilledCopiesTests(_Case):
    def _published(self) -> Image:
        image = self._pending("pin_images/done.jpg")
        Image.objects.filter(pk=image.pk).update(pending_scan=False)
        return image

    def test_a_storage_timeout_does_not_mark_the_photo_unreadable(self) -> None:
        image = self._published()
        with mock.patch(_THUMBNAIL, side_effect=_timeout()):
            tasks.generate_image_thumbnails([image.pk])
        self.assertIsNone(Image.objects.get(pk=image.pk).media_unreadable_at)

    def test_a_refusal_from_garage_does_not_mark_it_either(self) -> None:
        image = self._published()
        with mock.patch(_THUMBNAIL, side_effect=_garage_503()):
            tasks.generate_image_thumbnails([image.pk])
        self.assertIsNone(Image.objects.get(pk=image.pk).media_unreadable_at)

    def test_a_file_storage_says_is_gone_is_still_marked(self) -> None:
        image = self._published()
        with mock.patch(_THUMBNAIL, side_effect=FileNotFoundError("gone")):
            tasks.generate_image_thumbnails([image.pk])
        self.assertIsNotNone(Image.objects.get(pk=image.pk).media_unreadable_at)
