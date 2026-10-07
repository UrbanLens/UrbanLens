"""Pressing an expensive button twice must not run the job twice."""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core import single_flight


class TheClaimTests(TestCase):
    """The primitive."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_the_first_caller_gets_it(self) -> None:
        self.assertTrue(single_flight.claim("job:1", 60))

    def test_the_second_caller_does_not(self) -> None:
        single_flight.claim("job:1", 60)
        self.assertFalse(single_flight.claim("job:1", 60))

    def test_two_accounts_do_not_share_one(self) -> None:
        single_flight.claim("job:1", 60)
        self.assertTrue(single_flight.claim("job:2", 60), "one account's job blocked another account's")

    def test_releasing_lets_the_next_one_start(self) -> None:
        single_flight.claim("job:1", 60)
        single_flight.release("job:1")
        self.assertTrue(single_flight.claim("job:1", 60))

    def test_adopting_keeps_the_reservation_and_records_the_task(self) -> None:
        single_flight.claim("job:1", 60)
        single_flight.adopt("job:1", "task-abc", 60)
        self.assertEqual(single_flight.holder("job:1"), "task-abc")
        self.assertFalse(single_flight.claim("job:1", 60), "adopting released the reservation")

    def test_an_unreadable_cache_refuses(self) -> None:
        """The opposite of the throttle, deliberately: proceeding here would
        start a second copy of the most expensive work in the application."""
        with mock.patch.object(single_flight.cache, "add", side_effect=ConnectionError("dragonfly is gone")):
            self.assertFalse(single_flight.claim("job:1", 60))


class TheImmichScanButtonTests(TestCase):
    """A full-library sweep, which can hold a worker slot for the hard limit."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        self.client.force_login(self.user)
        baker.make(ImmichAccount, profile=self.profile)

    def _press(self):  # noqa: ANN202
        return self.client.post(reverse("settings.immich.scan"))

    def test_the_first_press_enqueues(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
        self.assertEqual(enqueue.call_count, 1)

    def test_the_second_press_does_not_enqueue_a_second_sweep(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
            self._press()
            self.assertEqual(enqueue.call_count, 1, "a second press enqueued a second full-library sweep")

    def test_the_second_press_still_answers_the_user(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
            response = self._press()
        self.assertEqual(response.status_code, 200, "the refused press errored instead of reporting the running scan")

    def test_a_failed_enqueue_does_not_hold_the_guard(self) -> None:
        """Otherwise a broker blip locks the button until the TTL expires."""
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task", return_value=None):
            self._press()
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-2")
            self._press()
            self.assertEqual(enqueue.call_count, 1, "the guard was left held after the enqueue failed")


class TheExportButtonTests(TestCase):
    """An export copies every photo the account owns, twice."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _press(self):  # noqa: ANN202
        return self.client.post(reverse("tools.export.start"), {"export_types": ["pins"]})

    @staticmethod
    def _export_calls(enqueue: mock.Mock) -> int:
        """Only the export task. A request enqueues other work too, and counting
        every call would make these assertions about unrelated notifications."""
        from urbanlens.dashboard.tasks import run_user_data_export

        return sum(1 for call in enqueue.call_args_list if call.args and call.args[0] is run_user_data_export)

    def test_the_first_press_enqueues(self) -> None:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
        self.assertEqual(self._export_calls(enqueue), 1)

    def test_the_second_press_does_not_enqueue_a_second_export(self) -> None:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
            self._press()
            self.assertEqual(self._export_calls(enqueue), 1, "a second press enqueued a second full-account export")

    def test_a_finished_export_releases_the_guard(self) -> None:
        """The status vocabulary is "pending"/"running"/"done"/"error". A guessed
        terminal set that missed "done" would hold the guard for its whole TTL
        after every successful export, which is the same bug wearing a fix."""
        from urbanlens.dashboard.services.import_export.export import ExportJobStatus

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
            job_id = single_flight.holder(f"ul:single-flight:export:{self.user.pk}")
            if not job_id or job_id == single_flight.PENDING:
                self.fail("the export never recorded its job id")

            ExportJobStatus(job_id).write("done", 100, "Export complete.", user_id=self.user.pk)
            self.client.get(reverse("tools.export.status", kwargs={"job_id": job_id}))

            self._press()
            self.assertEqual(self._export_calls(enqueue), 2, "a finished export still held the guard")

    def test_two_accounts_can_export_at_once(self) -> None:
        other = baker.make(User)
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            enqueue.return_value = mock.Mock(id="task-1")
            self._press()
            self.client.force_login(other)
            self._press()
            self.assertEqual(self._export_calls(enqueue), 2, "one account's export blocked another account's")


class TheImportButtonTests(TestCase):
    """An import extracts, scans and writes a whole archive, up to 500 MB of it, on a sandbox worker slot."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        media_root = tempfile.mkdtemp(prefix="ul_import_guard_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.enqueue = self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        self.enqueue.return_value = mock.Mock(id="task-1")

    def _press(self):  # noqa: ANN202
        return self.client.post(
            reverse("tools.import.start"),
            {
                "import_file": SimpleUploadedFile(
                    "export.zip", b"PK\x05\x06" + b"\0" * 18, content_type="application/zip"
                )
            },
        )

    def _import_calls(self) -> list[mock._Call]:
        """Only the import task, as for exports."""
        from urbanlens.dashboard.tasks import run_user_data_import

        return [call for call in self.enqueue.call_args_list if call.args and call.args[0] is run_user_data_import]

    def _guard(self) -> str | None:
        return single_flight.holder(f"ul:single-flight:import:{self.user.pk}")

    def test_the_first_press_enqueues(self) -> None:
        self._press()
        self.assertEqual(len(self._import_calls()), 1)

    def test_the_second_press_does_not_start_a_second_import(self) -> None:
        self._press()
        response = self._press()

        self.assertEqual(len(self._import_calls()), 1, "a second press enqueued a second import")
        self.assertEqual(response.status_code, 200, "the refused press errored instead of reporting the running import")
        self.assertContains(response, "An import is already running.")
        self.assertEqual(
            len(os.listdir(os.path.join(settings.MEDIA_ROOT, "imports"))), 1, "the refused press still saved its upload"
        )

    def test_a_failed_enqueue_does_not_hold_the_guard(self) -> None:
        self.enqueue.return_value = None
        self._press()
        self.enqueue.return_value = mock.Mock(id="task-2")
        self._press()

        self.assertEqual(len(self._import_calls()), 2, "the guard was left held after the enqueue failed")

    def test_a_finished_import_releases_the_guard_when_its_status_is_polled(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportJobStatus

        self._press()
        job_id = self._guard()
        if not job_id or job_id == single_flight.PENDING:
            self.fail("the import never recorded its job id")

        ImportJobStatus(job_id).write("done", 100, "Import complete!", user_id=self.user.pk)
        self.client.get(reverse("tools.import.status", kwargs={"job_id": job_id}))
        self._press()

        self.assertEqual(len(self._import_calls()), 2, "a finished import still held the guard")

    def test_the_import_task_releases_the_guard_however_it_ends(self) -> None:
        """A user who closes the tab never polls, so the job itself gives the claim up."""
        from urbanlens.dashboard.services.import_export.import_data import run_import

        self._press()
        user_id, zip_path, job_id = self._import_calls()[0].args[1:4]
        with mock.patch("urbanlens.dashboard.services.import_export.import_data.schedule_import_cleanup"):
            self.assertFalse(run_import(user_id, zip_path, job_id), "the stand-in archive imported")

        self.assertIsNone(self._guard(), "the finished import still held the guard")

    def test_the_import_task_keeps_the_guard_while_it_waits_for_storage(self) -> None:
        from urbanlens.dashboard.services.import_export import import_data

        self._press()
        user_id, zip_path, job_id = self._import_calls()[0].args[1:4]
        waiting = import_data.ImportWaitingForStorageError(resume={}, storage_waits=1, countdown=60, message="waiting")
        with (
            mock.patch.object(import_data, "_extract_and_validate", side_effect=waiting),
            self.assertRaises(import_data.ImportWaitingForStorageError),
        ):
            import_data.run_import(user_id, zip_path, job_id)

        self.assertEqual(self._guard(), job_id, "an import waiting to run again gave its claim up")

    def test_finishing_one_import_does_not_release_anothers_guard(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import run_import

        single_flight.adopt(f"ul:single-flight:import:{self.user.pk}", "a-newer-job", 60)
        with mock.patch("urbanlens.dashboard.services.import_export.import_data.schedule_import_cleanup"):
            run_import(self.user.pk, os.path.join(settings.MEDIA_ROOT, "missing.zip"), "an-older-job")

        self.assertEqual(self._guard(), "a-newer-job")

    def test_two_accounts_can_import_at_once(self) -> None:
        self._press()
        self.client.force_login(baker.make(User))
        self._press()

        self.assertEqual(len(self._import_calls()), 2, "one account's import blocked another account's")
