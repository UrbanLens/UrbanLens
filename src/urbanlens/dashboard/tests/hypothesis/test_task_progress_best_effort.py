"""Reporting progress must never fail the work that has already succeeded."""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.celery import update_task_progress


class UpdateTaskProgressTests(SimpleTestCase):
    def test_a_failing_backend_does_not_propagate(self) -> None:
        task = mock.Mock()
        task.update_state.side_effect = OSError("result backend unreachable")

        update_task_progress(task, current=1, total=2, message="working")

        self.assertTrue(task.update_state.called)

    def test_a_non_oserror_backend_failure_also_does_not_propagate(self) -> None:
        """redis-py raises its own ConnectionError, which is not an OSError - so
        narrowing this handler to OSError would leave the common case uncaught."""
        task = mock.Mock()
        task.update_state.side_effect = RuntimeError("kombu blew up")

        update_task_progress(task, current=1, total=2)

    def test_normal_progress_is_reported_with_a_percentage(self) -> None:
        task = mock.Mock()

        update_task_progress(task, current=1, total=4, message="working")

        meta = task.update_state.call_args.kwargs["meta"]
        self.assertEqual((meta["current"], meta["total"], meta["percent"]), (1, 4, 25))

    def test_a_zero_total_does_not_divide_by_zero(self) -> None:
        task = mock.Mock()

        update_task_progress(task, current=0, total=0)

        self.assertEqual(task.update_state.call_args.kwargs["meta"]["total"], 1)
