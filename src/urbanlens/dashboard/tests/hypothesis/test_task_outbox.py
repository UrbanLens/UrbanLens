"""The task outbox keeps enqueues the broker refused and queues them once it recovers."""

from __future__ import annotations

import contextlib
from datetime import timedelta
import importlib.util
import pathlib
from unittest import mock

from django.db import DatabaseError, transaction
from django.utils import timezone
from kombu.exceptions import OperationalError

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.task_outbox import TaskOutboxEntry
from urbanlens.dashboard.services.core.celery import safely_enqueue_task
from urbanlens.dashboard.services.core.task_outbox import drain_outbox

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _refusing(task):
    return mock.patch.object(task, "apply_async", side_effect=OperationalError("broker down"))


class RecordRefusedEnqueueTests(TestCase):
    def test_a_refused_durable_enqueue_is_kept_with_its_options(self) -> None:
        before = timezone.now()
        with _refusing(tasks.send_notification_text_alerts_if_unread):
            result = safely_enqueue_task(
                tasks.send_notification_text_alerts_if_unread, 42, countdown=300, expires=900, queue="interactive"
            )

        self.assertIsNone(result)
        entry = TaskOutboxEntry.objects.get()
        self.assertEqual(entry.task_name, tasks.send_notification_text_alerts_if_unread.name)
        self.assertEqual(entry.args, [42])
        self.assertEqual(entry.kwargs, {})
        self.assertEqual(entry.queue, "interactive")
        self.assertGreaterEqual(entry.not_before, before + timedelta(seconds=300))
        self.assertGreaterEqual(entry.expires_at, before + timedelta(seconds=900))
        self.assertLessEqual(entry.next_attempt_at, timezone.now())

    def test_keyword_arguments_are_kept(self) -> None:
        with _refusing(tasks.generate_boundaries_for_location):
            safely_enqueue_task(tasks.generate_boundaries_for_location, 7, force=True, attempt=2)

        entry = TaskOutboxEntry.objects.get()
        self.assertEqual((entry.args, entry.kwargs), ([7], {"force": True, "attempt": 2}))

    def test_a_non_durable_enqueue_is_not_kept(self) -> None:
        with _refusing(tasks.send_notification_text_alerts_if_unread):
            self.assertIsNone(safely_enqueue_task(tasks.send_notification_text_alerts_if_unread, 42, durable=False))

        self.assertFalse(TaskOutboxEntry.objects.exists())

    def test_the_entry_rolls_back_with_the_callers_transaction(self) -> None:
        with _refusing(tasks.recompute_fact_confidence), contextlib.suppress(RuntimeError), transaction.atomic():
            safely_enqueue_task(tasks.recompute_fact_confidence, 3)
            raise RuntimeError

        self.assertFalse(TaskOutboxEntry.objects.exists())

    def test_a_failed_write_is_logged_not_raised(self) -> None:
        with (
            _refusing(tasks.recompute_fact_confidence),
            mock.patch.object(TaskOutboxEntry.objects, "create", side_effect=DatabaseError("db down")),
            self.assertLogs("urbanlens.dashboard.services.core.task_outbox", "ERROR"),
        ):
            self.assertIsNone(safely_enqueue_task(tasks.recompute_fact_confidence, 3))

    def test_a_successful_enqueue_writes_nothing(self) -> None:
        with mock.patch.object(tasks.recompute_fact_confidence, "apply_async", return_value="ok"):
            self.assertEqual(safely_enqueue_task(tasks.recompute_fact_confidence, 3), "ok")

        self.assertFalse(TaskOutboxEntry.objects.exists())


class DrainOutboxTests(TestCase):
    def _entry(self, task, *args, **fields) -> TaskOutboxEntry:
        fields.setdefault("next_attempt_at", timezone.now() - timedelta(seconds=1))
        return TaskOutboxEntry.objects.create(task_name=task.name, args=list(args), **fields)

    def test_a_due_entry_is_queued_with_its_remaining_delay_and_deleted(self) -> None:
        now = timezone.now()
        self._entry(
            tasks.send_notification_text_alerts_if_unread,
            9,
            not_before=now + timedelta(seconds=120),
            expires_at=now + timedelta(hours=1),
            queue="interactive",
            kwargs={},
        )

        with mock.patch.object(tasks.send_notification_text_alerts_if_unread, "apply_async") as apply_async:
            self.assertEqual(drain_outbox(), 1)

        _, options = apply_async.call_args
        self.assertEqual(options["args"], [9])
        self.assertEqual(options["kwargs"], {})
        self.assertEqual(options["queue"], "interactive")
        self.assertTrue(100 <= options["countdown"] <= 121)
        self.assertIn("expires", options)
        self.assertFalse(TaskOutboxEntry.objects.exists())

    def test_an_entry_past_its_delay_runs_now(self) -> None:
        self._entry(tasks.recompute_fact_confidence, 1, not_before=timezone.now() - timedelta(minutes=5))

        with mock.patch.object(tasks.recompute_fact_confidence, "apply_async") as apply_async:
            drain_outbox()

        self.assertNotIn("countdown", apply_async.call_args.kwargs)

    def test_an_entry_not_yet_due_waits(self) -> None:
        self._entry(tasks.recompute_fact_confidence, 1, next_attempt_at=timezone.now() + timedelta(minutes=5))

        with mock.patch.object(tasks.recompute_fact_confidence, "apply_async") as apply_async:
            self.assertEqual(drain_outbox(), 0)

        apply_async.assert_not_called()
        self.assertTrue(TaskOutboxEntry.objects.exists())

    def test_a_refusal_stops_the_run_and_backs_the_entry_off(self) -> None:
        first = self._entry(tasks.recompute_fact_confidence, 1)
        self._entry(tasks.recompute_fact_confidence, 2)

        with _refusing(tasks.recompute_fact_confidence) as apply_async:
            self.assertEqual(drain_outbox(), 0)

        self.assertEqual(apply_async.call_count, 1)
        first.refresh_from_db()
        self.assertEqual(first.attempts, 1)
        self.assertGreater(first.next_attempt_at, timezone.now())
        self.assertIn("broker down", first.last_error)
        self.assertEqual(TaskOutboxEntry.objects.count(), 2)

    def test_an_expired_entry_is_dropped_unqueued(self) -> None:
        self._entry(tasks.recompute_fact_confidence, 1, expires_at=timezone.now() - timedelta(seconds=1))

        with mock.patch.object(tasks.recompute_fact_confidence, "apply_async") as apply_async:
            self.assertEqual(drain_outbox(), 0)

        apply_async.assert_not_called()
        self.assertFalse(TaskOutboxEntry.objects.exists())

    def test_an_unknown_task_is_dropped(self) -> None:
        TaskOutboxEntry.objects.create(
            task_name="urbanlens.dashboard.tasks.no_such_task", next_attempt_at=timezone.now()
        )

        with self.assertLogs("urbanlens.dashboard.services.core.task_outbox", "ERROR"):
            self.assertEqual(drain_outbox(), 0)

        self.assertFalse(TaskOutboxEntry.objects.exists())

    def test_refused_then_drained_reaches_the_task_once(self) -> None:
        with _refusing(tasks.recompute_fact_confidence):
            safely_enqueue_task(tasks.recompute_fact_confidence, 11)

        with mock.patch.object(tasks.recompute_fact_confidence, "apply_async") as apply_async:
            drain_outbox()
            drain_outbox()

        apply_async.assert_called_once()
        self.assertEqual(apply_async.call_args.kwargs["args"], [11])

    def test_the_task_skips_while_another_drain_holds_the_lock(self) -> None:
        self._entry(tasks.recompute_fact_confidence, 1)

        with (
            mock.patch("urbanlens.dashboard.services.core.locks.acquire_lock", return_value=None),
            mock.patch.object(tasks.recompute_fact_confidence, "apply_async") as apply_async,
        ):
            self.assertEqual(tasks.drain_task_outbox(), 0)

        apply_async.assert_not_called()

    def test_the_drain_is_on_the_beat_schedule(self) -> None:
        from django.conf import settings

        names = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn(tasks.drain_task_outbox.name, names)


class EnqueueDurabilityCheckTests(SimpleTestCase):
    def _check(self):
        spec = importlib.util.spec_from_file_location(
            "check_enqueue_durability", REPO_ROOT / "bin" / "check_enqueue_durability.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_tree_passes(self) -> None:
        self.assertEqual(self._check().main(), 0)

    def test_a_caller_using_the_result_without_durable_fails(self) -> None:
        check = self._check()
        path = self._tmp_file(check, "if safely_enqueue_task(task, 1) is None:\n    task(1)\n")
        self.assertEqual(len(check.offences_in(path)), 1)

    def test_an_aliased_import_is_still_checked(self) -> None:
        check = self._check()
        source = "from x import safely_enqueue_task as _enqueue\nresult = _enqueue(task, 1)\n"
        self.assertEqual(len(check.offences_in(self._tmp_file(check, source))), 1)

    def test_a_discarded_result_and_an_explicit_choice_pass(self) -> None:
        check = self._check()
        source = "safely_enqueue_task(task, 1)\nif safely_enqueue_task(task, 1, durable=False) is None:\n    pass\ntransaction.on_commit(lambda: safely_enqueue_task(task, 2))\n"
        self.assertEqual(check.offences_in(self._tmp_file(check, source)), [])

    def _tmp_file(self, check, source: str) -> pathlib.Path:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        check.REPO_ROOT = pathlib.Path(directory.name)
        path = check.REPO_ROOT / "probe.py"
        path.write_text(source)
        return path
