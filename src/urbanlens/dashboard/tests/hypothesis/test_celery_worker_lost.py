"""A task whose child dies must fail once, not be redelivered forever.

With ``task_acks_late`` on, Celery's failure handler has a branch that rejects
the message *with requeue* rather than acknowledging it. Two settings reach it,
and both turn a deterministic failure into an unbounded loop: the same message
goes straight back to a worker that fails the same way, and neither
``max_retries`` (which counts ``task.retry()`` calls, not broker deliveries) nor
the Redis/Valkey transport (which has no delivery limit) stops it.

The tests here drive the real :class:`celery.worker.request.Request` rather than
asserting on our own settings alone, because the behaviour that matters belongs
to Celery and kombu. That makes them a version pin: if a future Celery bounds
the redelivery itself - it already receives the ``redelivered`` flag kombu
stamps on restore, and currently ignores it - these fail and the settings can be
reconsidered rather than carried forever on stale reasoning.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

from billiard.einfo import ExceptionInfo
from billiard.exceptions import WorkerLostError
from celery import current_app, shared_task
from celery.contrib.testing.mocks import TaskMessage
from celery.exceptions import TimeLimitExceeded
from celery.worker.request import Request
from django.conf import settings
from django.core.checks import Error
from django.test import SimpleTestCase, override_settings
from kombu.transport import virtual

from urbanlens.dashboard.checks import check_celery_failures_cannot_requeue_forever

#: Tasks allowed to run with ``acks_late`` off, and why. Everything else inherits
#: ``CELERY_TASK_ACKS_LATE``; an entry here is a task that would rather be lost
#: than repeated, which is a per-task judgement rather than a default.
ACKS_LATE_EXEMPT = {
    # A turn that dies mid-loop must not be redelivered and re-spend a provider
    # call for a bubble the caller has already timed out on.
    "urbanlens.dashboard.services.ai.tasks.run_assistant_turn_task",
}

#: A floor on the discovered task count. The registry assertions are all "no task
#: does X", which an empty registry satisfies trivially - Celery discovers tasks
#: lazily, and simply reading ``app.tasks`` finds none of ours.
MINIMUM_DISCOVERED_TASKS = 50


@shared_task(name="tests.worker_lost_probe")
def worker_lost_probe() -> None:
    """A registered task to build a real Request around. Never executed."""


def _exception_info(exc: BaseException) -> ExceptionInfo:
    """Capture a real traceback for an exception, as the worker would.

    Args:
        exc: The exception to raise and capture.

    Returns:
        The captured :class:`~billiard.einfo.ExceptionInfo`.
    """
    try:
        raise exc
    except type(exc):
        return ExceptionInfo()


def _build_request(*, redelivered: bool = False) -> tuple[Request, Mock, Mock, Mock]:
    """Build a real Request for the probe task with observable callbacks.

    Args:
        redelivered: Whether the broker has already redelivered this message.
            Celery is handed the flag and is expected to ignore it.

    Returns:
        Tuple of (request, on_ack, on_reject, eventer).
    """
    message = TaskMessage(worker_lost_probe.name, args=(), kwargs={})
    # TaskMessage builds the message as a Mock, so delivery_info is assigned
    # outright rather than merged - a Mock attribute is not a mapping.
    message.delivery_info = {"exchange": "", "routing_key": "sandbox", "redelivered": redelivered}
    on_ack, on_reject, eventer = Mock(name="on_ack"), Mock(name="on_reject"), Mock(name="eventer")
    eventer.enabled = True
    request = Request(
        message,
        app=current_app,
        task=worker_lost_probe,
        on_ack=on_ack,
        on_reject=on_reject,
        eventer=eventer,
        hostname="test-worker",
    )
    return request, on_ack, on_reject, eventer


def _use_recording_backend(test: SimpleTestCase) -> Mock:
    """Swap the probe task's result backend for one that records calls.

    Assigned through ``Task.backend``'s own setter rather than ``patch.object``:
    the attribute is a property, so a patch would restore itself with a
    ``delattr`` the property cannot service. Setting it back to None restores
    the documented default of deferring to ``app.backend``.

    Args:
        test: The test case to register the cleanup on.

    Returns:
        The recording backend.
    """
    backend = Mock(name="backend")
    worker_lost_probe.backend = backend
    test.addCleanup(setattr, worker_lost_probe, "backend", None)
    return backend


def _event_types(eventer: Mock) -> list[str]:
    """Extract the event names an eventer was asked to send.

    Args:
        eventer: The mock event dispatcher.

    Returns:
        Event type strings in the order they were sent.
    """
    return [call.args[0] for call in eventer.send.call_args_list if call.args]


class WorkerLostRequeueTests(SimpleTestCase):
    """What Celery does with a task whose child process died."""

    def setUp(self) -> None:
        """Give the probe task a backend that records instead of storing."""
        super().setUp()
        self.backend = _use_recording_backend(self)

    def test_rejecting_on_worker_lost_requeues_even_a_redelivered_message(self) -> None:
        """The loop is real: nothing consults how many times this was delivered."""
        request, on_ack, on_reject, _ = _build_request(redelivered=True)
        with patch.object(worker_lost_probe, "acks_late", new=True), patch.object(worker_lost_probe, "reject_on_worker_lost", new=True):
            request.on_failure(_exception_info(WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL)")))

        on_reject.assert_called_once()
        self.assertIs(on_reject.call_args.args[-1], True, "the message was requeued despite already being a redelivery")
        on_ack.assert_not_called()

    def test_the_requeue_branch_reports_the_failure_nowhere(self) -> None:
        """Why the loop is silent: no stored result, no failure event."""
        request, _, on_reject, eventer = _build_request()
        with patch.object(worker_lost_probe, "acks_late", new=True), patch.object(worker_lost_probe, "reject_on_worker_lost", new=True):
            request.on_failure(_exception_info(WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL)")))

        on_reject.assert_called_once()
        self.backend.mark_as_failure.assert_not_called()
        self.assertNotIn("task-failed", _event_types(eventer))

    def test_our_setting_acknowledges_once_and_reports_the_failure(self) -> None:
        """With the setting off the loss becomes one visible failure."""
        request, on_ack, on_reject, eventer = _build_request()
        with patch.object(worker_lost_probe, "acks_late", new=True), patch.object(worker_lost_probe, "reject_on_worker_lost", new=False):
            request.on_failure(_exception_info(WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL)")))

        on_ack.assert_called_once()
        on_reject.assert_not_called()
        self.backend.mark_as_failure.assert_called_once()
        # The event the exporter in services/core/celery_events.py counts.
        self.assertIn("task-failed", _event_types(eventer))


class TimeLimitRequeueTests(SimpleTestCase):
    """The same unbounded branch, reached by a much likelier trigger."""

    def setUp(self) -> None:
        """Give the probe task a backend that records instead of storing."""
        super().setUp()
        _use_recording_backend(self)

    def test_a_timeout_requeues_when_acks_on_failure_or_timeout_is_off(self) -> None:
        """Every redelivery would exceed the same time limit again."""
        request, _, on_reject, _ = _build_request()
        with patch.object(worker_lost_probe, "acks_late", new=True), patch.object(worker_lost_probe, "acks_on_failure_or_timeout", new=False):
            request.on_failure(_exception_info(TimeLimitExceeded(3600)))

        on_reject.assert_called_once()
        self.assertIs(on_reject.call_args.args[-1], True)

    def test_a_timeout_is_acknowledged_under_our_settings(self) -> None:
        """Held at Celery's default, a timeout fails once."""
        request, on_ack, on_reject, _ = _build_request()
        with patch.object(worker_lost_probe, "acks_late", new=True), patch.object(worker_lost_probe, "acks_on_failure_or_timeout", new=True):
            request.on_failure(_exception_info(TimeLimitExceeded(3600)))

        on_ack.assert_called_once()
        on_reject.assert_not_called()


class RedeliveryIsImmediateTests(SimpleTestCase):
    """Why ``visibility_timeout`` does not pace the loop."""

    def test_restore_requeues_at_once_and_stamps_redelivered(self) -> None:
        """A rejected message is re-put immediately, carrying the flag Celery ignores."""
        channel = Mock(name="channel")
        channel._lookup.return_value = ["sandbox"]
        message = Mock(name="message")
        message.delivery_info = {"exchange": "", "routing_key": "sandbox"}
        message.serializable.return_value = {"body": "..."}

        virtual.Channel._restore(channel, message)

        channel._put.assert_called_once()
        queue, payload = channel._put.call_args.args
        self.assertEqual(queue, "sandbox")
        self.assertIs(payload["redelivered"], True)


class CeleryConfigurationReachesTasksTests(SimpleTestCase):
    """The Django settings have to actually bind onto the task classes.

    Celery copies ``task_*`` conf onto each task class once, at bind time, and
    only where the task has not set the attribute itself. Asserting on the
    settings alone would not notice a renamed setting, a namespace change, or a
    single task opting back in.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Force the autodiscovery that populates the task registry."""
        super().setUpClass()
        current_app.loader.import_default_modules()

    def _app_tasks(self) -> dict[str, Any]:
        """Every registered task except Celery's own built-ins.

        Returns:
            Mapping of task name to task instance.
        """
        return {name: task for name, task in current_app.tasks.items() if not name.startswith("celery.")}

    def test_the_registry_is_actually_populated(self) -> None:
        """Without this the three assertions below could all pass on nothing."""
        self.assertGreaterEqual(len(self._app_tasks()), MINIMUM_DISCOVERED_TASKS)

    def test_no_task_requeues_on_worker_lost(self) -> None:
        """Covers the global setting and any per-task override of it."""
        offenders = sorted(name for name, task in self._app_tasks().items() if getattr(task, "reject_on_worker_lost", False))
        self.assertEqual(offenders, [], f"these tasks requeue forever when their child is killed: {offenders}")

    def test_every_task_acknowledges_a_failure_or_timeout(self) -> None:
        """The second route into the same unbounded branch."""
        offenders = sorted(name for name, task in self._app_tasks().items() if not getattr(task, "acks_on_failure_or_timeout", True))
        self.assertEqual(offenders, [], f"these tasks requeue forever on a time limit: {offenders}")

    def test_acks_late_is_off_only_where_that_is_deliberate(self) -> None:
        """The fix must not have been achieved by giving up at-least-once delivery.

        Dropping ``acks_late`` would also end the loop - by acknowledging every
        message before running it, so any lost worker loses the task outright.
        That is a much larger change than the one this module is about, and it
        would be easy to arrive at accidentally.
        """
        offenders = sorted(name for name, task in self._app_tasks().items() if not getattr(task, "acks_late", False))
        self.assertEqual(sorted(set(offenders) - ACKS_LATE_EXEMPT), [], f"these tasks acknowledge before running, so a lost worker loses them: {offenders}")


class RequeueSystemCheckTests(SimpleTestCase):
    """``dashboard.E007`` / ``dashboard.E008`` guard the settings."""

    def _ids(self, errors: list[Error]) -> list[str]:
        """Extract check ids.

        Args:
            errors: Messages returned by the check.

        Returns:
            The id of each message.
        """
        return [error.id for error in errors]

    def test_current_settings_pass(self) -> None:
        """The shipped configuration raises nothing."""
        self.assertEqual(check_celery_failures_cannot_requeue_forever(), [])

    @override_settings(CELERY_TASK_ACKS_LATE=True, CELERY_TASK_REJECT_ON_WORKER_LOST=True)
    def test_reject_on_worker_lost_is_refused(self) -> None:
        """Restoring the setting fails startup."""
        self.assertIn("dashboard.E007", self._ids(check_celery_failures_cannot_requeue_forever()))

    @override_settings(CELERY_TASK_ACKS_LATE=True, CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT=False)
    def test_acks_on_failure_or_timeout_is_refused(self) -> None:
        """The timeout route is refused too."""
        self.assertIn("dashboard.E008", self._ids(check_celery_failures_cannot_requeue_forever()))

    @override_settings(CELERY_TASK_ACKS_LATE=False, CELERY_TASK_REJECT_ON_WORKER_LOST=True)
    def test_neither_matters_without_acks_late(self) -> None:
        """Celery ignores both settings unless the ack is late."""
        self.assertEqual(check_celery_failures_cannot_requeue_forever(), [])

    def test_the_settings_the_check_reads_exist(self) -> None:
        """A check reading a setting production does not define proves nothing."""
        for name in ("CELERY_TASK_ACKS_LATE", "CELERY_TASK_REJECT_ON_WORKER_LOST", "CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT"):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name), f"{name} is not defined in settings")
