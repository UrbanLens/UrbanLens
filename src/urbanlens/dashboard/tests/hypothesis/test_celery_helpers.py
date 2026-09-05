"""Property-based tests for shared Celery task helpers."""

from __future__ import annotations

from unittest import mock

from kombu.exceptions import KombuError

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.celery import (
    PROGRESS_STATE,
    TaskProgress,
    get_task_progress,
    safely_enqueue_task,
    update_task_progress,
)


class TaskProgressTests(SimpleTestCase):
    """TaskProgress serializes consistently for polling clients."""

    @given(state=st.sampled_from(["PENDING", "STARTED", "PROGRESS", "SUCCESS", "FAILURE", "REVOKED"]))
    @hyp_settings(max_examples=20)
    def test_ready_matches_terminal_states(self, state: str) -> None:
        payload = TaskProgress(task_id="task-1", state=state).as_dict()
        self.assertEqual(payload["ready"], state in {"SUCCESS", "FAILURE", "REVOKED"})


class UpdateTaskProgressTests(SimpleTestCase):
    """update_task_progress clamps unsafe inputs and computes percentages."""

    @given(
        current=st.integers(min_value=-10_000, max_value=10_000), total=st.integers(min_value=-100, max_value=10_000)
    )
    @hyp_settings(max_examples=50)
    def test_clamps_current_and_total(self, current: int, total: int) -> None:
        task = mock.Mock()

        update_task_progress(task, current=current, total=total, message="Working")

        task.update_state.assert_called_once()
        _, kwargs = task.update_state.call_args
        meta = kwargs["meta"]
        expected_total = max(total or 1, 1)
        expected_current = max(0, min(current or 0, expected_total))
        expected_percent = int((expected_current / expected_total) * 100)
        self.assertEqual(kwargs["state"], PROGRESS_STATE)
        self.assertEqual(meta["current"], expected_current)
        self.assertEqual(meta["total"], expected_total)
        self.assertEqual(meta["percent"], expected_percent)
        self.assertEqual(meta["message"], "Working")

    def test_swallows_update_state_exception(self) -> None:
        task = mock.Mock()
        task.update_state.side_effect = RuntimeError("backend unreachable")

        with mock.patch("urbanlens.dashboard.services.core.celery.logger") as mock_logger:
            update_task_progress(task, current=1, total=2, message="Working")

        task.update_state.assert_called_once()
        self.assertTrue(mock_logger.warning.called)


class GetTaskProgressTests(SimpleTestCase):
    """get_task_progress normalizes Celery result backend states."""

    def test_success_uses_result_payload(self) -> None:
        result = mock.Mock(state="SUCCESS", result={"ok": True}, info={})
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.percent, 100)
        self.assertEqual(progress.result, {"ok": True})

    def test_failure_exposes_error_string(self) -> None:
        result = mock.Mock(state="FAILURE", result=RuntimeError("boom"), info={})
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.state, "FAILURE")
        self.assertIn("boom", progress.error)

    def test_progress_state_reads_metadata(self) -> None:
        result = mock.Mock(state="PROGRESS", info={"current": 2, "total": 4, "percent": 50, "message": "Halfway"})
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.current, 2)
        self.assertEqual(progress.total, 4)
        self.assertEqual(progress.percent, 50)
        self.assertEqual(progress.message, "Halfway")

    def test_revoked_uses_error_payload(self) -> None:
        result = mock.Mock(state="REVOKED", result=None, info="cancelled by user")
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.state, "REVOKED")
        self.assertEqual(progress.error, "cancelled by user")

    def test_failure_falls_back_to_default_message(self) -> None:
        result = mock.Mock(state="FAILURE", result=None, info=None)
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.error, "Task failed")

    def test_non_dict_info_defaults_progress_fields(self) -> None:
        """Celery reports ``info=None`` for a task still PENDING; the non-dict guard must catch it."""
        result = mock.Mock(state="PENDING", info=None)
        with mock.patch("urbanlens.dashboard.services.core.celery.AsyncResult", return_value=result):
            progress = get_task_progress("task-1")
        self.assertEqual(progress.current, 0)
        self.assertEqual(progress.total, 1)
        self.assertEqual(progress.percent, 0)
        self.assertEqual(progress.message, "")


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

    def test_uses_apply_async_with_expires(self) -> None:
        # First-class, not folded into **kwargs: those are task arguments, not
        # apply_async options - see the assistant turn task (batch 2c), the
        # first caller that needs a stale turn dropped rather than run late.
        task = mock.Mock()
        task.apply_async.return_value = "async-result"
        self.assertEqual(safely_enqueue_task(task, 1, expires=120), "async-result")
        task.apply_async.assert_called_once_with(args=(1,), kwargs={}, expires=120)

    def test_expires_omitted_when_not_given(self) -> None:
        task = mock.Mock()
        safely_enqueue_task(task, 1)
        self.assertNotIn("expires", task.apply_async.call_args.kwargs)

    def test_returns_none_on_broker_exception(self) -> None:
        task = mock.Mock(name="broken_task")
        task.apply_async.side_effect = KombuError("broker down")
        self.assertIsNone(safely_enqueue_task(task))

    @given(exception=st.sampled_from([ConnectionError("down"), OSError("down"), RuntimeError("down")]))
    @hyp_settings(max_examples=3)
    def test_returns_none_on_other_caught_exceptions(self, exception: Exception) -> None:
        task = mock.Mock(name="broken_task")
        task.apply_async.side_effect = exception
        self.assertIsNone(safely_enqueue_task(task))

    def test_reraises_exceptions_outside_broker_scope(self) -> None:
        task = mock.Mock(name="broken_task")
        task.apply_async.side_effect = ValueError("not a broker error")
        with self.assertRaises(ValueError):
            safely_enqueue_task(task)
