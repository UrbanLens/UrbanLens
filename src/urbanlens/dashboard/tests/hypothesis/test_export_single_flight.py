"""An export's single-flight claim belongs to its job from the start, and nothing that stops the job keeps it held.

``test_single_flight_jobs`` covers one export at a time; these cover the claim's two gaps: a failure between taking
it and queueing the job left it held for its whole TTL, and it held "pending" rather than the job, so a job could not
tell its own claim from another's.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.core import single_flight


class AnExportsClaimTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        media_root = tempfile.mkdtemp(prefix="ul_export_guard_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.enqueue = self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        self.enqueue.return_value = mock.Mock(id="task-1")

    def _press(self):  # noqa: ANN202
        return self.client.post(reverse("tools.export.start"), {"export_types": ["pins"]})

    def _export_calls(self) -> list[mock._Call]:
        from urbanlens.dashboard.tasks import run_user_data_export

        return [call for call in self.enqueue.call_args_list if call.args and call.args[0] is run_user_data_export]

    def _guard(self) -> str | None:
        return single_flight.holder(f"ul:single-flight:export:{self.user.pk}")

    def test_a_failure_before_the_enqueue_does_not_hold_the_guard(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.tools.os.makedirs", side_effect=OSError("volume full")):
            self._press()
        self._press()

        self.assertEqual(len(self._export_calls()), 1, "the guard was left held after the export could not start")

    def test_the_claim_names_its_job_from_the_start(self) -> None:
        """A refused press polls the running job; "pending" is not one, and the poll errored."""
        seen: list[str | None] = []

        def enqueue(*args: object, **_kwargs: object) -> mock.Mock:
            seen.append(self._guard())
            return mock.Mock(id="task-1")

        self.enqueue.side_effect = enqueue

        self._press()
        response = self._press()

        job_id = self._export_calls()[0].args[5]
        self.assertEqual(seen[0], job_id, "the claim held something other than its job while the job was queued")
        self.assertEqual(self._guard(), job_id)
        self.assertContains(response, f"/export/status/{job_id}/")

    def test_the_export_task_releases_the_guard_however_it_ends(self) -> None:
        """A user who closes the tab never polls, so the job gives the claim up itself."""
        from urbanlens.dashboard.services.import_export.export import run_export

        self._press()
        user_id, export_types, export_dir, base_url, job_id = self._export_calls()[0].args[1:6]
        with (
            mock.patch("urbanlens.dashboard.services.import_export.export.schedule_export_cleanup"),
            mock.patch(
                "urbanlens.dashboard.services.import_export.export._run_export_steps", side_effect=RuntimeError("boom")
            ),
        ):
            self.assertFalse(run_export(user_id, export_types, export_dir, base_url, job_id=job_id))

        self.assertIsNone(self._guard(), "the finished export still held the guard")

    def test_an_export_that_ends_before_the_view_returns_gives_its_claim_up(self) -> None:
        from urbanlens.dashboard.services.import_export.export import run_export

        def run_at_once(task, user_id, export_types, export_dir, base_url, job_id, email_to_user, **_kwargs):  # noqa: ANN001, ANN202, ARG001, PLR0917 - safely_enqueue_task's shape
            with (
                mock.patch("urbanlens.dashboard.services.import_export.export.schedule_export_cleanup"),
                mock.patch("urbanlens.dashboard.services.import_export.export._run_export_steps"),
            ):
                run_export(user_id, export_types, export_dir, base_url, job_id=job_id)
            return mock.Mock(id="task-eager")

        self.enqueue.side_effect = run_at_once
        self._press()

        self.assertIsNone(self._guard(), "an export that had already ended still held the guard")

    def test_polling_an_older_export_does_not_release_a_newer_ones_claim(self) -> None:
        from urbanlens.dashboard.services.import_export.export import ExportJobStatus

        single_flight.adopt(f"ul:single-flight:export:{self.user.pk}", "a-newer-job", 60)
        old = "11111111-1111-4111-8111-111111111111"
        ExportJobStatus(old).write("done", 100, "Export ready", user_id=self.user.pk)

        self.client.get(reverse("tools.export.status", kwargs={"job_id": old}))

        self.assertEqual(self._guard(), "a-newer-job")
