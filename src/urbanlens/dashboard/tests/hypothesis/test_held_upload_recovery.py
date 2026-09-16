"""A held icon or avatar whose publish never ran is recovered, and a held file nothing can name again is removed (P119).

``safely_enqueue_task`` returns None when the broker is unreachable, so without a sweep a held upload whose enqueue
failed is never published: the owner is told it is processing for ever, and the file stays under ``unprocessed/``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import suppress
from datetime import timedelta
import io
from itertools import count
import os
import shutil
import tempfile
import time
from typing import IO
from unittest import mock
import uuid
import zlib

from botocore.awsrequest import AWSRequest, AWSResponse
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ParamValidationError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from redis.exceptions import ConnectionError as RedisConnectionError

from urbanlens.core.cache_backend import ResilientRedisCache
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import TripComment
from urbanlens.dashboard.models.undo.model import UNDO_RETENTION
from urbanlens.dashboard.services.media.held_upload import (
    HELD_FIELDS,
    HELD_PREFIX,
    STORAGE_ERRORS,
    held_rows,
    hold_upload,
    queue_held_upload,
    sweep_held_uploads,
)
from urbanlens.dashboard.services.media.object_storage import GatedS3Storage
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import SANDBOX, _fixtures
from urbanlens.UrbanLens.settings.base import _S3_STORAGE_OPTIONS

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_REENCODE = "urbanlens.dashboard.services.media.images.reencode_image_file"
_SWEEP = "urbanlens.dashboard.tasks.sweep_held_uploads"
_SWEEP_UNNAMED = "urbanlens.dashboard.tasks.sweep_unnamed_files"
_SHOWN_DIRECTORIES = ("avatars", "label_icons", "pin_custom_icons", "achievement_icons", "comment_images")
_owners = count()
_HOUR = 3600

#: What storage raises when it cannot do its job: the filesystem's OSError, and the S3 backend's botocore errors, of
#: which only the timeouts derive from OSError.
_STORAGE_FAILURES = (
    PermissionError("storage unavailable"),
    EndpointConnectionError(endpoint_url="http://objectstore:3900"),
    ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce your request rate."}}, "PutObject"),
)


def _age(name: str, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(default_storage.path(name), (past, past))


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_held_recovery_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=media_root)
        media.enable()
        self.addCleanup(media.disable)

    def _profile(self) -> Profile:
        return User.objects.create(username=f"recoveryowner{next(_owners)}").profile

    def _held_while_the_broker_was_down(self, profile: Profile) -> str:
        with mock.patch(_ENQUEUE, return_value=None), self.captureOnCommitCallbacks(execute=True):
            profile.save(update_fields=[hold_upload(profile, "avatar", ContentFile(_fixtures()["png-text"][1]))])
            queue_held_upload(profile, "avatar")
        return profile.avatar_upload

    def _sweep(self) -> mock.MagicMock:
        self.assertIn(
            _SWEEP, [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()], "nothing sweeps held uploads"
        )
        with mock.patch(_ENQUEUE) as enqueue:
            getattr(tasks, _SWEEP.rsplit(".", 1)[1])()
        return enqueue

    def _publishes(self, enqueue: mock.MagicMock) -> list[tuple]:
        return [call.args for call in enqueue.call_args_list if call.args and call.args[0] is tasks.publish_held_upload]


class AHeldUploadWhoseEnqueueFailedTests(_Case):
    def test_the_sweep_queues_its_publish(self) -> None:
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        self.assertEqual(
            self._publishes(self._sweep()), [(tasks.publish_held_upload, "dashboard.Profile.avatar", profile.pk, held)]
        )

    def test_one_the_worker_may_still_be_reaching_is_left_alone(self) -> None:
        self._held_while_the_broker_was_down(self._profile())

        self.assertEqual(self._publishes(self._sweep()), [])

    def test_one_that_kills_the_worker_is_dropped_rather_than_fed_to_it_for_ever(self) -> None:
        """A publish that never finishes never reaches the task's own give-up, so the sweep has to stop queueing it."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        started = 0
        for _ in range(10):
            for _task, *args in self._publishes(self._sweep()):
                started += 1
                with (
                    override_settings(**SANDBOX),
                    mock.patch(_REENCODE, side_effect=MemoryError),
                    self.assertRaises(MemoryError),
                ):
                    tasks.publish_held_upload(*args)

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertFalse(default_storage.exists(held))
        self.assertLess(started, 10)

    def test_a_backed_up_worker_does_not_cost_an_upload_its_publish(self) -> None:
        """Queued many times but never started is a queue behind a bulk import, not a file that kills the worker."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        for _ in range(10):
            self._sweep()

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, held)
        self.assertTrue(default_storage.exists(held))

    def test_a_publish_retried_through_a_storage_outage_is_still_published(self) -> None:
        """A start that ended in a storage error handed itself to a retry; it finished, so it is not a killed worker."""
        key = "dashboard.Profile.avatar"
        for error in _STORAGE_FAILURES:
            with self.subTest(error=type(error).__name__):
                profile = self._profile()
                held = self._held_while_the_broker_was_down(profile)
                _age(held, _HOUR)

                for attempt in range(4):
                    with (
                        override_settings(**SANDBOX),
                        mock.patch.object(FileSystemStorage, "save", side_effect=error),
                        mock.patch.object(tasks.publish_held_upload, "retry", side_effect=error) as retry,
                        self.assertRaises(
                            type(error), msg=f"attempt {attempt} published nothing, so the upload was already dropped"
                        ),
                    ):
                        tasks.publish_held_upload(key, profile.pk, held)
                    self.assertTrue(retry.called, f"attempt {attempt} failed outright instead of being retried")
                    self._sweep()

                with override_settings(**SANDBOX):
                    self.assertTrue(tasks.publish_held_upload(key, profile.pk, held))
                profile.refresh_from_db()
                self.assertEqual(profile.avatar_upload, "")
                self.assertTrue(profile.avatar)

    def test_a_publish_still_decoding_when_the_sweep_runs_is_not_dropped(self) -> None:
        """Two starts a deploy killed, then a third the sweep finds mid-decode: that one may yet finish."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)
        key = "dashboard.Profile.avatar"
        for _ in range(2):
            with (
                override_settings(**SANDBOX),
                mock.patch(_REENCODE, side_effect=MemoryError),
                self.assertRaises(MemoryError),
            ):
                tasks.publish_held_upload(key, profile.pk, held)

        from urbanlens.dashboard.services.media import images

        decode = images.reencode_image_file

        def swept_mid_decode(
            stored_file: IO[bytes], *, max_dimension: int | None, convert_webp: bool
        ) -> tuple[bytes, str]:
            self._sweep()
            return decode(stored_file, max_dimension=max_dimension, convert_webp=convert_webp)

        with override_settings(**SANDBOX), mock.patch(_REENCODE, side_effect=swept_mid_decode):
            published = tasks.publish_held_upload(key, profile.pk, held)

        self.assertTrue(published, "the sweep dropped the upload while its publish was decoding it")
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertTrue(profile.avatar)

    def test_a_duplicate_publish_ending_early_does_not_expose_the_one_still_decoding(self) -> None:
        """A duplicate queued while the worker was backed up overlaps the publish, and hands itself to a retry first."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)
        key = "dashboard.Profile.avatar"
        for _ in range(2):
            with (
                override_settings(**SANDBOX),
                mock.patch(_REENCODE, side_effect=MemoryError),
                self.assertRaises(MemoryError),
            ):
                tasks.publish_held_upload(key, profile.pk, held)

        from urbanlens.dashboard.services.media import images

        decode = images.reencode_image_file

        def duplicate_then_sweep(
            stored_file: IO[bytes], *, max_dimension: int | None, convert_webp: bool
        ) -> tuple[bytes, str]:
            with (
                mock.patch(_REENCODE, side_effect=decode),
                mock.patch.object(FileSystemStorage, "save", side_effect=OSError("storage unavailable")),
                suppress(OSError),
            ):
                tasks.publish_held_upload(key, profile.pk, held)
            self._sweep()
            return decode(stored_file, max_dimension=max_dimension, convert_webp=convert_webp)

        with override_settings(**SANDBOX), mock.patch(_REENCODE, side_effect=duplicate_then_sweep):
            published = tasks.publish_held_upload(key, profile.pk, held)

        self.assertTrue(published, "the sweep dropped the upload while its first publish was still decoding it")
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertTrue(profile.avatar)

    def test_a_publish_redelivered_after_a_cold_shutdown_is_published(self) -> None:
        """A cold shutdown hands a running publish back unacknowledged, and the child it killed never cleared the mark."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        args = ("dashboard.Profile.avatar", profile.pk, held)
        with (
            override_settings(**SANDBOX),
            mock.patch(_REENCODE, side_effect=MemoryError),
            mock.patch.object(LocMemCache, "delete", return_value=False),
            self.assertRaises(MemoryError),
        ):
            tasks.publish_held_upload.apply(args=args, task_id="delivery", throw=True)

        with override_settings(**SANDBOX):
            duplicate = tasks.publish_held_upload.apply(args=args, task_id="duplicate", throw=True).get()
            self.assertFalse(duplicate, "the killed delivery left no mark, so nothing here tests a redelivery")
            redelivered = tasks.publish_held_upload.apply(args=args, task_id="delivery", throw=True).get()

        self.assertTrue(redelivered, "the redelivered publish took its own killed delivery for a duplicate")
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertTrue(profile.avatar)

    def test_a_publish_goes_ahead_while_the_cache_is_down(self) -> None:
        """The running mark is a lock nothing can take in an outage; waiting on it would hold every upload."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        down = ResilientRedisCache("redis://127.0.0.1:6379/0", {"OPTIONS": {}})
        outage = [
            mock.patch.object(RedisCache, method, side_effect=RedisConnectionError("dragonfly is down"))
            for method in ("get", "set", "add", "delete")
        ]
        with outage[0], outage[1], outage[2], outage[3], mock.patch("django.core.cache.cache", down):
            self.assertFalse(down.add("held-upload-probe", 1), "the cache under test is not down")
            self.assertIsNone(down.get("held-upload-probe"), "the cache under test is not down")
            with override_settings(**SANDBOX), mock.patch.object(down, "add", wraps=down.add) as add:
                published = tasks.publish_held_upload("dashboard.Profile.avatar", profile.pk, held)

        self.assertIn(
            f"held-upload-running:{held}",
            [call.args[0] for call in add.call_args_list],
            "the publish never asked the cache that is down",
        )
        self.assertTrue(published, "a publish waited on a lock the cache could not hold")
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertTrue(profile.avatar)

    def test_an_upload_waiting_for_storage_is_left_to_the_retry_sweep(self) -> None:
        """The retry sweep paces it; queueing it here every hour as well would undo that pacing."""
        from urbanlens.dashboard.models.upload_retry import UploadRetry

        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)
        UploadRetry.objects.create(
            target="dashboard.Profile.avatar", object_id=profile.pk, name=held, next_attempt_at=timezone.now()
        )

        self.assertEqual(self._publishes(self._sweep()), [])

    def test_an_upload_whose_file_is_gone_waits_rather_than_being_dropped(self) -> None:
        """Storage pointed at the wrong place reports every file gone, so one gone file is not proof of loss."""
        from urbanlens.dashboard.models.upload_retry import UploadRetry

        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        default_storage.delete(held)

        self.assertEqual(self._publishes(self._sweep()), [])

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, held)
        waiting = UploadRetry.objects.get(target="dashboard.Profile.avatar", object_id=profile.pk)
        self.assertIsNotNone(waiting.gone_since)

    def test_a_sweep_during_a_broker_outage_reports_nothing_queued(self) -> None:
        """The count is what an operator reads mid-incident, and an enqueue the broker refused queued nothing."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        with mock.patch(_ENQUEUE, return_value=None) as enqueue:
            handled, _removed = sweep_held_uploads()

        self.assertTrue(self._publishes(enqueue), "the sweep never tried to queue the stalled upload")
        self.assertEqual(handled, 0, "the sweep counted a publish the broker refused as queued")

    def test_one_file_storage_cannot_stat_does_not_stop_the_rest_being_recovered(self) -> None:
        real = FileSystemStorage.get_modified_time
        for error in _STORAGE_FAILURES:
            with self.subTest(error=type(error).__name__):
                unreadable, recoverable = self._profile(), self._profile()
                broken = self._held_while_the_broker_was_down(unreadable)
                held = self._held_while_the_broker_was_down(recoverable)
                for name in (broken, held):
                    _age(name, _HOUR)

                def stat(storage: FileSystemStorage, name: str, broken: str = broken, error: Exception = error):
                    if name == broken:
                        raise error
                    return real(storage, name)

                with mock.patch.object(FileSystemStorage, "get_modified_time", stat):
                    queued = self._publishes(self._sweep())

                self.assertIn((tasks.publish_held_upload, "dashboard.Profile.avatar", recoverable.pk, held), queued)

    def test_a_misconfigured_storage_client_is_not_retried_as_an_outage(self) -> None:
        """A bad parameter or missing credentials is not storage being down; retrying only delays and quiets it."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        for error in (ParamValidationError(report="Invalid bucket name"), NoCredentialsError()):
            with self.subTest(error=type(error).__name__):
                with (
                    override_settings(**SANDBOX),
                    mock.patch.object(FileSystemStorage, "save", side_effect=error),
                    mock.patch.object(tasks.publish_held_upload, "retry", side_effect=error) as retry,
                    self.assertRaises(type(error)),
                ):
                    tasks.publish_held_upload("dashboard.Profile.avatar", profile.pk, held)
                self.assertFalse(retry.called, "a misconfigured storage client was retried as though storage were down")

    def test_a_sweep_whose_storage_client_cannot_authenticate_fails_loudly(self) -> None:
        """Missing credentials are every file's problem, and a warning per file skipped hides that."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        with (
            mock.patch.object(FileSystemStorage, "exists", side_effect=NoCredentialsError()),
            self.assertRaises(NoCredentialsError),
        ):
            self._sweep()

    def test_a_held_file_a_row_still_names_is_never_removed_as_left_behind(self) -> None:
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, (UNDO_RETENTION.days + 2) * 24 * _HOUR)

        self._sweep()

        self.assertTrue(default_storage.exists(held))


class AHeldFileNothingNamesTests(_Case):
    def _orphan(self, age: float) -> str:
        name = default_storage.save(f"{HELD_PREFIX}/{uuid.uuid4().hex}", ContentFile(b"left behind"))
        _age(name, age)
        return name

    def test_it_is_removed_once_no_undo_could_restore_it(self) -> None:
        expired = self._orphan((UNDO_RETENTION.days + 2) * 24 * _HOUR)
        restorable = self._orphan((UNDO_RETENTION.days - 1) * 24 * _HOUR)

        self._sweep()

        self.assertEqual((default_storage.exists(expired), default_storage.exists(restorable)), (False, True))

    def test_one_storage_refused_to_delete_is_not_reported_removed(self) -> None:
        for error in _STORAGE_FAILURES:
            with self.subTest(error=type(error).__name__):
                expired = self._orphan((UNDO_RETENTION.days + 2) * 24 * _HOUR)

                with mock.patch(_ENQUEUE), mock.patch.object(FileSystemStorage, "delete", side_effect=error):
                    _handled, removed = sweep_held_uploads()

                self.assertTrue(default_storage.exists(expired), "storage deleted the file, so nothing was refused")
                self.assertEqual(removed, 0, "the sweep counted a file storage refused to delete as removed")
                default_storage.delete(expired)


class AShownFileNothingNamesTests(_Case):
    """An icon, avatar or comment image no row names is removed, however it was left: a delete storage refused, a label
    or pin icon a publish replaced, a row deleted outside undo. The media gate serves any icon or avatar path."""

    _OLD = (UNDO_RETENTION.days + 2) * 24 * _HOUR

    def _file(self, directory: str, age: float) -> str:
        name = default_storage.save(f"{directory}/{uuid.uuid4().hex}.webp", ContentFile(b"shown once"))
        _age(name, age)
        return name

    def _sweep_unnamed(self) -> None:
        self.assertIn(
            _SWEEP_UNNAMED,
            [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()],
            "nothing sweeps unnamed files",
        )
        getattr(tasks, _SWEEP_UNNAMED.rsplit(".", 1)[1])()

    def test_every_directory_a_shown_image_is_stored_in_is_swept(self) -> None:
        for directory in _SHOWN_DIRECTORIES:
            with self.subTest(directory=directory):
                name = self._file(directory, self._OLD)

                self._sweep_unnamed()

                self.assertFalse(default_storage.exists(name))

    def test_a_file_a_row_names_is_kept(self) -> None:
        profile = self._profile()
        name = self._file("avatars", self._OLD)
        Profile.objects.filter(pk=profile.pk).update(avatar=name)

        self._sweep_unnamed()

        self.assertTrue(default_storage.exists(name))

    def test_a_file_another_model_names_in_the_same_directory_is_kept(self) -> None:
        name = self._file("comment_images", self._OLD)
        baker.make(TripComment, image=name)

        self._sweep_unnamed()

        self.assertTrue(default_storage.exists(name))

    def test_a_file_saved_for_a_row_that_has_not_committed_yet_is_kept(self) -> None:
        name = self._file("label_icons", 60)

        self._sweep_unnamed()

        self.assertTrue(default_storage.exists(name))

    def test_an_icon_an_undo_could_restore_is_kept_until_the_undo_expires(self) -> None:
        from urbanlens.dashboard.models.undo.model import UndoAction
        from urbanlens.dashboard.services.undo.service import stash_for_undo

        profile = self._profile()
        name = self._file("label_icons", self._OLD)
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzSwept Icon")
        Label.objects.filter(pk=label.pk).update(custom_icon=name)
        label.refresh_from_db()
        undo_action = stash_for_undo("label", [label], profile)
        assert undo_action is not None, "nothing was stashed"
        label.delete()

        self._sweep_unnamed()
        self.assertTrue(default_storage.exists(name), "the icon of a label undo can restore was removed")

        UndoAction.objects.filter(pk=undo_action.pk).update(
            created=timezone.now() - UNDO_RETENTION - timedelta(hours=1)
        )
        self._sweep_unnamed()
        self.assertFalse(default_storage.exists(name), "the icon of a label no undo can restore stayed served")

    def test_one_file_storage_refused_to_delete_does_not_stop_the_rest(self) -> None:
        names = [self._file(directory, self._OLD) for directory in _SHOWN_DIRECTORIES]
        original = FileSystemStorage.delete
        refused: list[str] = []

        def delete(storage: FileSystemStorage, name: str) -> None:
            if not refused:
                refused.append(name)
                raise _STORAGE_FAILURES[2]
            original(storage, name)

        with mock.patch.object(FileSystemStorage, "delete", delete):
            self._sweep_unnamed()

        self.assertEqual([name for name in names if default_storage.exists(name)], refused)

    def test_one_directory_storage_cannot_list_does_not_stop_the_rest(self) -> None:
        names = [self._file(directory, self._OLD) for directory in _SHOWN_DIRECTORIES]
        original = FileSystemStorage.listdir
        refused: list[str] = []

        def listdir(storage: FileSystemStorage, path: str) -> tuple[list[str], list[str]]:
            if not refused:
                refused.append(path)
                raise _STORAGE_FAILURES[1]
            return original(storage, path)

        with mock.patch.object(FileSystemStorage, "listdir", listdir):
            self._sweep_unnamed()

        self.assertEqual(len([name for name in names if default_storage.exists(name)]), 1)


class _RawBody(io.BytesIO):
    def stream(self, amt: int = 1024, decode_content: bool | None = None) -> Iterator[bytes]:
        while chunk := self.read(amt):
            yield chunk


class TheS3BackendsFailuresAreStorageErrorsTests(SimpleTestCase):
    """The failure tests above patch FileSystemStorage; this holds the real S3 backend to raising what they raise."""

    def _storage(self) -> GatedS3Storage:
        return GatedS3Storage(
            **{
                **_S3_STORAGE_OPTIONS,
                "bucket_name": "held",
                "access_key": "test",
                "secret_key": "test",
                "region_name": "us-east-1",
                "endpoint_url": "http://objectstore:3900",
            },
        )

    def test_every_operation_the_held_path_uses_fails_with_a_storage_error(self) -> None:
        name = f"{HELD_PREFIX}/{uuid.uuid4().hex}"
        operations = (
            ("open", "head_object", lambda storage: storage.open(name, "rb")),
            ("exists", "head_object", lambda storage: storage.exists(name)),
            ("get_modified_time", "head_object", lambda storage: storage.get_modified_time(name)),
            ("delete", "delete_object", lambda storage: storage.delete(name)),
            ("listdir", "list_objects", lambda storage: storage.listdir(HELD_PREFIX)),
        )
        for label, method, call in operations:
            for code, status in (("AccessDenied", 403), ("SlowDown", 503)):
                with self.subTest(operation=label, status=status):
                    storage = self._storage()
                    with Stubber(storage.connection.meta.client) as stub:
                        stub.add_client_error(method, service_error_code=code, http_status_code=status)
                        with self.assertRaises(STORAGE_ERRORS):
                            call(storage)
                        stub.assert_no_pending_responses()

    def test_a_save_the_object_store_rejects_fails_with_a_storage_error(self) -> None:
        """A publish's write is checked by the object store, which rejects a body that fails its checksum with a 400."""
        for code, status in (("BadDigest", 400), ("SlowDown", 503)):
            with self.subTest(code=code):
                storage = self._storage()
                with Stubber(storage.connection.meta.client) as stub:
                    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
                    stub.add_client_error("put_object", service_error_code=code, http_status_code=status)
                    with self.assertRaises(STORAGE_ERRORS):
                        storage.save(f"{HELD_PREFIX}/{uuid.uuid4().hex}", ContentFile(b"re-encoded"))
                    stub.assert_no_pending_responses()

    def test_a_read_the_download_gives_up_on_fails_with_a_storage_error(self) -> None:
        """Opening only checks the object is there; reading downloads it, and s3transfer retries a broken download itself."""
        name = f"{HELD_PREFIX}/{uuid.uuid4().hex}"
        storage = self._storage()
        attempts = storage.transfer_config.num_download_attempts
        with Stubber(storage.connection.meta.client) as stub:
            for _ in range(2):
                stub.add_response("head_object", {"ContentLength": 10, "ETag": '"held"'})
            for _ in range(attempts):
                stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(b"short"), 10), "ContentLength": 10})
            with self.assertRaises(STORAGE_ERRORS), storage.open(name, "rb") as handle:
                handle.read()
            stub.assert_no_pending_responses()

    def test_a_missing_object_reads_as_missing(self) -> None:
        """A gone file is told apart from storage refusing, which the task retries sooner."""
        name = f"{HELD_PREFIX}/{uuid.uuid4().hex}"
        storage = self._storage()
        with Stubber(storage.connection.meta.client) as stub:
            stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
            stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
            self.assertFalse(storage.exists(name))
            with self.assertRaises(FileNotFoundError):
                storage.open(name, "rb")
            stub.assert_no_pending_responses()

    def test_a_download_that_arrives_corrupt_fails_with_a_storage_error(self) -> None:
        """botocore checks a download against the checksum the object store sends, and s3transfer does not retry a mismatch."""
        storage = self._storage()
        sent: list[str] = []

        def respond(request: AWSRequest, **_: object) -> AWSResponse:
            sent.append(request.method)
            other = base64.b64encode(zlib.crc32(b"something else").to_bytes(4, "big")).decode()
            headers = {"Content-Length": "10", "ETag": '"held"', "x-amz-checksum-crc32": other}
            return AWSResponse(request.url, 200, headers, _RawBody(b"0123456789" if request.method == "GET" else b""))

        storage.connection.meta.client.meta.events.register("before-send.s3", respond)
        with self.assertRaises(STORAGE_ERRORS), storage.open(f"{HELD_PREFIX}/{uuid.uuid4().hex}", "rb") as handle:
            handle.read()
        self.assertIn("GET", sent)


class TheSweepsLookupTests(_Case):
    def test_finding_held_uploads_does_not_read_every_row(self) -> None:
        """The sweep runs hourly and almost nothing is held, so it must not scan the pin table to learn that."""
        for held in HELD_FIELDS.values():
            sql, params = held_rows(held).query.sql_with_params()
            with self.subTest(held.key), connection.cursor() as cursor:
                cursor.execute("SET LOCAL enable_seqscan = off")
                cursor.execute(f"EXPLAIN {sql}", params)
                plan = "\n".join(row[0] for row in cursor.fetchall())
                self.assertNotIn("Seq Scan", plan)


class ARowSavedAsACopyTests(_Case):
    def test_a_loaded_row_saved_with_no_primary_key_is_inserted(self) -> None:
        """Django's copy idiom: a column list cannot be forced onto a row that has no primary key."""
        original = Label.objects.create(profile=self._profile(), kind=KIND_TAG, name="ZzCopy Original")
        copy = Label.objects.get(pk=original.pk)
        copy.pk = None
        copy.uuid = uuid.uuid4()
        copy.name = "ZzCopy Copy"

        copy.save()

        self.assertEqual(Label.objects.filter(name__startswith="ZzCopy").count(), 2)
