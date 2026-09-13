"""A held icon or avatar whose publish never ran is recovered, and a held file nothing can name again is removed (P119).

``safely_enqueue_task`` returns None when the broker is unreachable, so without a sweep a held upload whose enqueue
failed is never published: the owner is told it is processing for ever, and the file stays under ``unprocessed/``.
"""

from __future__ import annotations

from itertools import count
import os
import shutil
import tempfile
import time
from unittest import mock
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connection
from django.test import override_settings

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UNDO_RETENTION
from urbanlens.dashboard.services.media.held_upload import (
    HELD_FIELDS,
    HELD_PREFIX,
    held_rows,
    hold_upload,
    queue_held_upload,
)
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import SANDBOX, _fixtures

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_REENCODE = "urbanlens.dashboard.services.media.images.reencode_image_file"
_SWEEP = "urbanlens.dashboard.tasks.sweep_held_uploads"
_owners = count()
_HOUR = 3600


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
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)
        key = "dashboard.Profile.avatar"

        for attempt in range(4):
            with (
                override_settings(**SANDBOX),
                mock.patch.object(FileSystemStorage, "save", side_effect=OSError("storage unavailable")),
                self.assertRaises(
                    OSError, msg=f"attempt {attempt} published nothing, so the upload was already dropped"
                ),
            ):
                tasks.publish_held_upload(key, profile.pk, held)
            self._sweep()

        with override_settings(**SANDBOX):
            self.assertTrue(tasks.publish_held_upload(key, profile.pk, held))
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertTrue(profile.avatar)

    def test_one_file_storage_cannot_stat_does_not_stop_the_rest_being_recovered(self) -> None:
        unreadable, recoverable = self._profile(), self._profile()
        broken = self._held_while_the_broker_was_down(unreadable)
        held = self._held_while_the_broker_was_down(recoverable)
        for name in (broken, held):
            _age(name, _HOUR)
        real = FileSystemStorage.get_modified_time

        def stat(storage: FileSystemStorage, name: str):
            if name == broken:
                raise PermissionError(name)
            return real(storage, name)

        with mock.patch.object(FileSystemStorage, "get_modified_time", stat):
            queued = self._publishes(self._sweep())

        self.assertIn((tasks.publish_held_upload, "dashboard.Profile.avatar", recoverable.pk, held), queued)

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
