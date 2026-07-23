"""Property-based tests for shared Celery task helpers."""

from __future__ import annotations

from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st
from kombu.exceptions import KombuError

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.celery import PROGRESS_STATE, TaskProgress, get_task_progress, safely_enqueue_task, update_task_progress


class TaskProgressTests(SimpleTestCase):
    """TaskProgress serializes consistently for polling clients."""

    @given(state=st.sampled_from(["PENDING", "STARTED", "PROGRESS", "SUCCESS", "FAILURE", "REVOKED"]))
    @hyp_settings(max_examples=20)
    def test_ready_matches_terminal_states(self, state: str) -> None:
        payload = TaskProgress(task_id="task-1", state=state).as_dict()
        self.assertEqual(payload["ready"], state in {"SUCCESS", "FAILURE", "REVOKED"})


class UpdateTaskProgressTests(SimpleTestCase):
    """update_task_progress clamps unsafe inputs and computes percentages."""

    @given(current=st.integers(min_value=-10_000, max_value=10_000), total=st.integers(min_value=-100, max_value=10_000))
    @hyp_settings(max_examples=50)
    def test_clamps_current_and_total(self, current: int, total: int) -> None:
        task = mock.Mock()

        update_task_progress(task, current=current, total=total, message="Working")

        task.update_state.assert_called_once()
        _, kwargs = task.update_state.call_args
        meta = kwargs["meta"]
        self.assertEqual(kwargs["state"], PROGRESS_STATE)
        self.assertGreaterEqual(meta["current"], 0)
        self.assertGreaterEqual(meta["total"], 1)
        self.assertLessEqual(meta["current"], meta["total"])
        self.assertGreaterEqual(meta["percent"], 0)
        self.assertLessEqual(meta["percent"], 100)
        self.assertEqual(meta["message"], "Working")


class GetTaskProgressTests(SimpleTestCase):
    """get_task_progress normalizes Celery result backend states."""

    def test_success_uses_result_payload(self) -> None:
        result = mock.Mock(state="SUCCESS", result={"ok": True}, info={})
        with mock.patch("urbanlens.dashboard.services.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.percent, 100)
        self.assertEqual(progress.result, {"ok": True})

    def test_failure_exposes_error_string(self) -> None:
        result = mock.Mock(state="FAILURE", result=RuntimeError("boom"), info={})
        with mock.patch("urbanlens.dashboard.services.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.state, "FAILURE")
        self.assertIn("boom", progress.error)

    def test_progress_state_reads_metadata(self) -> None:
        result = mock.Mock(state="PROGRESS", info={"current": 2, "total": 4, "percent": 50, "message": "Halfway"})
        with mock.patch("urbanlens.dashboard.services.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.current, 2)
        self.assertEqual(progress.total, 4)
        self.assertEqual(progress.percent, 50)
        self.assertEqual(progress.message, "Halfway")


class SafelyEnqueueTaskTests(SimpleTestCase):
    """safely_enqueue_task delegates to Celery and handles broker errors."""

    def test_uses_apply_async_without_countdown(self) -> None:
        task = mock.Mock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(safely_enqueue_task(task, 1, named=True), "async-result")
        task.apply_async.assert_called_once_with(args=(1,), kwargs={"named": True})

    def test_uses_apply_async_with_countdown(self) -> None:
        task = mock.Mock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(safely_enqueue_task(task, 1, countdown=30, named=True), "async-result")
        task.apply_async.assert_called_once_with(args=(1,), kwargs={"named": True}, countdown=30)

    def test_uses_apply_async_with_queue(self) -> None:
        task = mock.Mock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(safely_enqueue_task(task, 1, queue="panel_fetch"), "async-result")
        task.apply_async.assert_called_once_with(args=(1,), kwargs={}, queue="panel_fetch")

    def test_returns_none_on_broker_exception(self) -> None:
        task = mock.Mock(name="broken_task")
        task.apply_async.side_effect = KombuError("broker down")
        self.assertIsNone(safely_enqueue_task(task))
