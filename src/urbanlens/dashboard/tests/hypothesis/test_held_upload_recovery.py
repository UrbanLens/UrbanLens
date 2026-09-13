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
from django.core.files.storage import default_storage
from django.test import override_settings

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UNDO_RETENTION
from urbanlens.dashboard.services.media.held_upload import HELD_PREFIX, hold_upload, queue_held_upload

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
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
            profile.save(update_fields=[hold_upload(profile, "avatar", ContentFile(b"an avatar"))])
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

    def test_one_that_keeps_stalling_is_dropped_rather_than_queued_for_ever(self) -> None:
        """A file that kills the worker never reaches the task's own give-up, so the sweep has to stop feeding it."""
        profile = self._profile()
        held = self._held_while_the_broker_was_down(profile)
        _age(held, _HOUR)

        queued = [len(self._publishes(self._sweep())) for _ in range(10)]

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertFalse(default_storage.exists(held))
        self.assertLess(sum(queued), 10, queued)


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
