"""An upload storage keeps failing on waits for storage to recover, rather than being dropped or rejected (P119).

The upload is already in media storage, so nothing more is needed from its owner. Waiting must not cost anyone else:
attempts back off, a sweep queues a bounded batch, and while storage fails for everyone only one upload probes it. One
that keeps failing while others succeed is reported to the admins, since retrying may never fix it.
"""

from __future__ import annotations

from datetime import timedelta
import io
from itertools import count
import shutil
import tempfile
from unittest import mock
import uuid

from botocore.exceptions import ClientError
from celery.exceptions import Retry
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage
from redis.exceptions import RedisError

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.models.upload_retry import UploadRetry
from urbanlens.dashboard.services.media import upload_retry
from urbanlens.dashboard.services.media.held_upload import hold_upload, queue_held_upload
from urbanlens.dashboard.services.notifications import notifications
from urbanlens.dashboard.services.security import malware_scan
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import SANDBOX, _fixtures

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_NOTIFY = "urbanlens.dashboard.services.notifications.notifications.notify"
_AVATAR = "dashboard.Profile.avatar"
_owners = count()

_STORAGE_FAILURES = (
    PermissionError("storage unavailable"),
    ClientError(
        {"Error": {"Code": "SlowDown", "Message": "busy"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "PutObject"
    ),
    ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "PutObject",
    ),
)


def _png() -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="PNG")
    return SimpleUploadedFile("photo.png", buf.getvalue(), content_type="image/png")


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_upload_retry_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=media_root)
        media.enable()
        self.addCleanup(media.disable)
        cache.clear()

    def _profile(self) -> Profile:
        return User.objects.create(username=f"retryowner{next(_owners)}").profile

    def _held(self, profile: Profile) -> str:
        with mock.patch(_ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            profile.save(update_fields=[hold_upload(profile, "avatar", ContentFile(_fixtures()["png-text"][1]))])
            queue_held_upload(profile, "avatar")
        return profile.avatar_upload

    def _pending_comment(self) -> Comment:
        profile = self._profile()
        return Comment.objects.create(
            pin=baker.make(Pin, profile=profile), profile=profile, text="waiting", image=_png(), pending_scan=True
        )

    def _waiting(self, target: str, object_id: int, name: str = "", *, due: bool = True) -> UploadRetry:
        offset = timedelta(minutes=-1 if due else 30)
        return UploadRetry.objects.create(
            target=target, object_id=object_id, name=name or uuid.uuid4().hex, next_attempt_at=timezone.now() + offset
        )

    def _rejected(self, comment: Comment) -> bool:
        return NotificationLog.objects.filter(
            profile=comment.profile, notification_type=NotificationType.COMMENT_UPLOAD_FAILED
        ).exists()


class AStorageFailureThatOutlastsTheTasksRetriesTests(_Case):
    def test_a_held_upload_waits_for_storage_instead_of_being_dropped(self) -> None:
        for error in _STORAGE_FAILURES:
            with self.subTest(error=str(error)):
                profile = self._profile()
                held = self._held(profile)

                with (
                    override_settings(**SANDBOX),
                    mock.patch.object(FileSystemStorage, "save", side_effect=error),
                    mock.patch.object(tasks.publish_held_upload, "max_retries", 0),
                ):
                    self.assertFalse(tasks.publish_held_upload(_AVATAR, profile.pk, held))

                profile.refresh_from_db()
                self.assertEqual(profile.avatar_upload, held, "the upload was dropped")
                self.assertTrue(default_storage.exists(held))
                self.assertTrue(UploadRetry.objects.filter(target=_AVATAR, object_id=profile.pk, name=held).exists())

    def test_a_comment_waits_for_storage_instead_of_being_rejected(self) -> None:
        for error in _STORAGE_FAILURES:
            with self.subTest(error=str(error)):
                comment = self._pending_comment()

                with (
                    mock.patch.object(malware_scan, "malware_error_for_upload", return_value=None),
                    mock.patch.object(FileSystemStorage, "save", side_effect=error),
                    mock.patch.object(tasks.scan_comment_image, "max_retries", 0),
                ):
                    self.assertFalse(tasks.scan_comment_image(comment.pk))

                self.assertTrue(Comment.objects.filter(pk=comment.pk, pending_scan=True).exists(), "it was rejected")
                self.assertFalse(self._rejected(comment))
                self.assertTrue(
                    UploadRetry.objects.filter(target="dashboard.Comment.image", object_id=comment.pk).exists()
                )

    def test_a_trip_comment_waits_for_storage_instead_of_being_rejected(self) -> None:
        profile = self._profile()
        comment = TripComment.objects.create(
            trip=baker.make(Trip, creator=profile), author=profile, text="waiting", image=_png(), pending_scan=True
        )

        with (
            mock.patch.object(malware_scan, "malware_error_for_upload", return_value=None),
            mock.patch.object(FileSystemStorage, "save", side_effect=_STORAGE_FAILURES[1]),
            mock.patch.object(tasks.scan_trip_comment_image, "max_retries", 0),
        ):
            self.assertFalse(tasks.scan_trip_comment_image(comment.pk))

        self.assertTrue(TripComment.objects.filter(pk=comment.pk, pending_scan=True).exists(), "it was rejected")
        self.assertTrue(UploadRetry.objects.filter(target="dashboard.TripComment.image", object_id=comment.pk).exists())

    def test_storage_refusing_the_read_for_the_malware_scan_is_not_the_scanner_being_down(self) -> None:
        """The scan reads the image from storage; a refusal there rejected the comment as an antivirus outage."""
        comment = self._pending_comment()
        scanner = mock.Mock(instream=mock.Mock(return_value={"stream": ("OK", None)}))

        with (
            mock.patch.object(malware_scan.app_settings, "clamav_enabled", True),
            mock.patch.object(malware_scan, "_client", return_value=scanner),
            mock.patch.object(FileSystemStorage, "open", side_effect=PermissionError("storage unavailable")),
            mock.patch.object(tasks.scan_comment_image, "max_retries", 0),
        ):
            self.assertFalse(tasks.scan_comment_image(comment.pk))

        self.assertTrue(Comment.objects.filter(pk=comment.pk, pending_scan=True).exists(), "it was rejected")
        self.assertFalse(self._rejected(comment))
        self.assertTrue(UploadRetry.objects.filter(target="dashboard.Comment.image", object_id=comment.pk).exists())

    def _gone_past_the_grace(self, target: str, object_id: int) -> None:
        UploadRetry.objects.filter(target=target, object_id=object_id).update(
            gone_since=timezone.now() - upload_retry.GONE_GRACE - timedelta(hours=1)
        )

    def test_a_held_upload_whose_file_stays_gone_while_storage_works_is_dropped(self) -> None:
        """Storage pointed at the wrong bucket says every file is gone, so one gone file is not dropped the first time."""
        profile = self._profile()
        held = self._held(profile)

        with (
            override_settings(**SANDBOX),
            mock.patch.object(FileSystemStorage, "open", side_effect=FileNotFoundError(held)),
            mock.patch.object(tasks.publish_held_upload, "retry", side_effect=Retry()) as retry,
        ):
            self.assertFalse(tasks.publish_held_upload(_AVATAR, profile.pk, held))
            profile.refresh_from_db()
            self.assertEqual(profile.avatar_upload, held, "dropped the first time storage said its file was gone")
            waiting = UploadRetry.objects.get(target=_AVATAR, object_id=profile.pk)
            self.assertGreater(waiting.next_attempt_at, timezone.now() + upload_retry.RETRY_CAP - timedelta(minutes=1))

            self._gone_past_the_grace(_AVATAR, profile.pk)
            self.assertFalse(tasks.publish_held_upload(_AVATAR, profile.pk, held))
            profile.refresh_from_db()
            self.assertEqual(profile.avatar_upload, held, "dropped though storage has served nothing since")

            upload_retry.record_storage_success()
            self.assertFalse(tasks.publish_held_upload(_AVATAR, profile.pk, held))

        retry.assert_not_called()
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertFalse(UploadRetry.objects.exists())

    def test_a_comment_whose_image_stays_gone_while_storage_works_is_rejected(self) -> None:
        comment = self._pending_comment()

        with (
            mock.patch.object(malware_scan, "malware_error_for_upload", return_value=None),
            mock.patch.object(FileSystemStorage, "open", side_effect=FileNotFoundError(comment.image.name)),
            mock.patch.object(tasks.scan_comment_image, "retry", side_effect=Retry()) as retry,
        ):
            self.assertFalse(tasks.scan_comment_image(comment.pk))
            self.assertTrue(Comment.objects.filter(pk=comment.pk, pending_scan=True).exists())

            self._gone_past_the_grace("dashboard.Comment.image", comment.pk)
            upload_retry.record_storage_success()
            self.assertFalse(tasks.scan_comment_image(comment.pk))

        retry.assert_not_called()
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
        self.assertTrue(self._rejected(comment))
        self.assertFalse(UploadRetry.objects.exists())

    def test_an_upload_already_waiting_goes_straight_back_to_waiting(self) -> None:
        """The sweep paces a waiting upload; the task's own quick retries would multiply every attempt it queues."""
        profile = self._profile()
        held = self._held(profile)
        waiting = self._waiting(_AVATAR, profile.pk, held)

        with (
            override_settings(**SANDBOX),
            mock.patch.object(FileSystemStorage, "save", side_effect=_STORAGE_FAILURES[1]),
            mock.patch.object(tasks.publish_held_upload, "retry", side_effect=Retry()) as retry,
        ):
            self.assertFalse(tasks.publish_held_upload(_AVATAR, profile.pk, held))

        retry.assert_not_called()
        self.assertTrue(UploadRetry.objects.filter(pk=waiting.pk).exists())

    def test_a_publish_that_succeeds_stops_waiting(self) -> None:
        profile = self._profile()
        held = self._held(profile)
        self._waiting(_AVATAR, profile.pk, held)

        with override_settings(**SANDBOX):
            self.assertTrue(tasks.publish_held_upload(_AVATAR, profile.pk, held))

        self.assertFalse(UploadRetry.objects.exists())

    def test_a_comment_deleted_while_waiting_stops_waiting(self) -> None:
        comment = self._pending_comment()
        self._waiting("dashboard.Comment.image", comment.pk, comment.image.name)
        comment_id = comment.pk
        comment.delete()

        self.assertFalse(tasks.scan_comment_image(comment_id))

        self.assertFalse(UploadRetry.objects.exists())


class WhichStorageErrorsSayTheFileIsGoneTests(SimpleTestCase):
    def test_a_missing_file_but_not_a_refusal_or_a_missing_bucket(self) -> None:
        def client_error(code: str, status: int) -> ClientError:
            return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")

        permanent = [FileNotFoundError("gone"), client_error("NoSuchKey", 404), client_error("404", 404)]
        transient = [
            PermissionError("unavailable"),
            client_error("SlowDown", 503),
            client_error("AccessDenied", 403),
            client_error("NoSuchBucket", 404),
            client_error("InternalError", 500),
        ]
        self.assertEqual([upload_retry.means_file_is_gone(error) for error in permanent], [True] * 3)
        self.assertEqual([upload_retry.means_file_is_gone(error) for error in transient], [False] * 5)


class TheRetrySweepTests(_Case):
    def _sweep(self) -> list[tuple]:
        with mock.patch(_ENQUEUE) as enqueue:
            tasks.retry_waiting_uploads()
        return [call.args for call in enqueue.call_args_list]

    def test_it_runs_on_a_schedule(self) -> None:
        scheduled = [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()]
        self.assertIn("urbanlens.dashboard.tasks.retry_waiting_uploads", scheduled)
        self.assertIn("urbanlens.dashboard.tasks.adopt_stalled_comment_scans", scheduled)
        self.assertEqual(tasks.retry_waiting_uploads.queue, "maintenance")

    def test_due_uploads_are_queued_oldest_first_and_no_more_than_a_batch(self) -> None:
        upload_retry.record_storage_success()
        now = timezone.now()
        waiting = [
            UploadRetry.objects.create(
                target=_AVATAR, object_id=pk, name=f"n{pk}", next_attempt_at=now - timedelta(minutes=pk)
            )
            for pk in range(1, upload_retry.RETRY_BATCH + 6)
        ]
        self._waiting(_AVATAR, 999_999, due=False)

        queued = self._sweep()

        oldest = sorted(waiting, key=lambda retry: retry.next_attempt_at)[: upload_retry.RETRY_BATCH]
        self.assertEqual(
            queued, [(tasks.publish_held_upload, _AVATAR, retry.object_id, retry.name) for retry in oldest]
        )

    def test_each_attempt_waits_twice_as_long_as_the_one_before_until_a_day(self) -> None:
        upload_retry.record_storage_success()
        waiting = self._waiting(_AVATAR, 1)
        intervals = []
        for _ in range(12):
            UploadRetry.objects.filter(pk=waiting.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            before = timezone.now()
            self._sweep()
            waiting.refresh_from_db()
            intervals.append(round((waiting.next_attempt_at - before).total_seconds() / 60))

        expected = [min(intervals[0] * 2**step, upload_retry.RETRY_CAP.total_seconds() / 60) for step in range(12)]
        self.assertEqual(intervals, expected)
        self.assertEqual(intervals[-1], upload_retry.RETRY_CAP.total_seconds() / 60)

    def test_it_queues_every_kind_of_upload_to_its_own_task(self) -> None:
        upload_retry.record_storage_success()
        self._waiting(_AVATAR, 1, "held-avatar")
        self._waiting("dashboard.Label.custom_icon", 2, "held-icon")
        self._waiting("dashboard.Comment.image", 3)
        self._waiting("dashboard.TripComment.image", 4)

        self.assertCountEqual(
            self._sweep(),
            [
                (tasks.publish_held_upload, _AVATAR, 1, "held-avatar"),
                (tasks.publish_held_upload, "dashboard.Label.custom_icon", 2, "held-icon"),
                (tasks.scan_comment_image, 3),
                (tasks.scan_trip_comment_image, 4),
            ],
        )

    def test_while_storage_fails_for_everyone_one_upload_probes_it(self) -> None:
        for pk in range(1, 6):
            self._waiting(_AVATAR, pk)
        upload_retry.record_storage_success()
        upload_retry.wait_for_storage(_AVATAR, 1, "n1", _STORAGE_FAILURES[1])
        UploadRetry.objects.filter(object_id=1).update(next_attempt_at=timezone.now() - timedelta(minutes=1))

        self.assertEqual(len(self._sweep()), 1)

        upload_retry.record_storage_success()
        self.assertEqual(len(self._sweep()), 4)

    def test_a_gone_file_is_not_storage_failing_for_everyone(self) -> None:
        for pk in range(1, 6):
            self._waiting(_AVATAR, pk)
        upload_retry.record_storage_success()

        self.assertFalse(upload_retry.file_is_gone(_AVATAR, 99, "gone", FileNotFoundError("gone")))

        self.assertEqual(len(self._sweep()), 5)

    def test_a_cache_outage_still_leaves_the_upload_waiting(self) -> None:
        """The stamps only pace the sweep; losing them must not lose the upload or fail a publish that landed."""
        with (
            mock.patch.object(cache, "get", side_effect=RedisError("down")),
            mock.patch.object(cache, "set", side_effect=RedisError("down")),
        ):
            upload_retry.wait_for_storage(_AVATAR, 1, "n1", _STORAGE_FAILURES[1])
            upload_retry.record_storage_success()
            UploadRetry.objects.update(next_attempt_at=timezone.now() - timedelta(minutes=1))
            self.assertEqual(len(self._sweep()), 1)

        self.assertTrue(UploadRetry.objects.filter(target=_AVATAR, object_id=1).exists())

    def test_an_attempt_the_broker_would_not_take_is_not_queued_again_until_it_is_due(self) -> None:
        upload_retry.record_storage_success()
        waiting = self._waiting(_AVATAR, 1)

        with mock.patch(_ENQUEUE, return_value=None):
            self.assertEqual(tasks.retry_waiting_uploads(), 0)

        waiting.refresh_from_db()
        self.assertGreater(waiting.next_attempt_at, timezone.now())
        self.assertEqual(self._sweep(), [])


class AnUploadThatKeepsFailingWhileOthersSucceedTests(_Case):
    def _stuck(self, object_id: int = 1) -> UploadRetry:
        waiting = self._waiting(_AVATAR, object_id, due=False)
        UploadRetry.objects.filter(pk=waiting.pk).update(
            created=timezone.now() - upload_retry.STUCK_AFTER - timedelta(hours=1),
            updated=timezone.now() - timedelta(hours=1),
        )
        return waiting

    def test_the_admins_are_told_once(self) -> None:
        waiting = self._stuck()
        upload_retry.record_storage_success()

        with mock.patch(_ENQUEUE), mock.patch(_NOTIFY) as notify:
            tasks.retry_waiting_uploads()
            tasks.retry_waiting_uploads()

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], notifications.NotificationEvent.UPLOAD_STUCK)
        self.assertIn(str(waiting.object_id), notify.call_args.args[2])
        waiting.refresh_from_db()
        self.assertIsNotNone(waiting.admin_notified_at)

    def test_nothing_is_sent_while_storage_fails_for_everyone(self) -> None:
        self._stuck()
        upload_retry.record_storage_success()
        upload_retry.wait_for_storage(_AVATAR, 2, "other", _STORAGE_FAILURES[1])

        with mock.patch(_ENQUEUE), mock.patch(_NOTIFY) as notify:
            tasks.retry_waiting_uploads()

        notify.assert_not_called()

    def test_nothing_is_sent_for_an_upload_that_has_not_waited_long(self) -> None:
        self._waiting(_AVATAR, 1, due=False)
        upload_retry.record_storage_success()

        with mock.patch(_ENQUEUE), mock.patch(_NOTIFY) as notify:
            tasks.retry_waiting_uploads()

        notify.assert_not_called()

    def test_the_alert_can_be_routed(self) -> None:
        channels = notifications._EVENT_CHANNEL_FIELDS[notifications.NotificationEvent.UPLOAD_STUCK]
        for field in channels.values():
            self.assertTrue(SiteSettings._meta.get_field(field))

    def test_an_admin_can_give_up_on_one(self) -> None:
        profile = self._profile()
        held = self._held(profile)
        comment = self._pending_comment()
        self._waiting(_AVATAR, profile.pk, held)
        self._waiting("dashboard.Comment.image", comment.pk, comment.image.name)

        for waiting in UploadRetry.objects.all():
            upload_retry.give_up(waiting)

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertFalse(default_storage.exists(held))
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
        self.assertTrue(self._rejected(comment))
        self.assertFalse(UploadRetry.objects.exists())


class ACommentScanThatNeverRanTests(_Case):
    def test_it_is_picked_up_to_wait_for_a_retry(self) -> None:
        stalled, recent, already = self._pending_comment(), self._pending_comment(), self._pending_comment()
        Comment.objects.filter(pk__in=[stalled.pk, already.pk]).update(
            created=timezone.now() - upload_retry.STALLED_SCAN_AGE - timedelta(minutes=5)
        )
        self._waiting("dashboard.Comment.image", already.pk, due=False)

        tasks.adopt_stalled_comment_scans()

        waiting = dict(UploadRetry.objects.values_list("object_id", "next_attempt_at"))
        self.assertIn(stalled.pk, waiting)
        self.assertLessEqual(waiting[stalled.pk], timezone.now())
        self.assertNotIn(recent.pk, waiting)
        self.assertEqual(UploadRetry.objects.filter(object_id=already.pk).count(), 1)
        self.assertGreater(waiting[already.pk], timezone.now(), "adopting it again reset its backoff")
