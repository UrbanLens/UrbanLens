"""Every task has time limits sized to its queue, and a soft limit cannot be swallowed by ``except Exception``."""

from __future__ import annotations

import os
import signal
import time
from types import SimpleNamespace
from unittest import mock

from billiard import pool
from celery.exceptions import SoftTimeLimitExceeded

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.checks import check_every_task_has_a_time_limit
from urbanlens.dashboard.services.core import task_limits
from urbanlens.dashboard.services.sandbox.queues import Queue
from urbanlens.UrbanLens.celery import app as celery_app


def _our_tasks():
    return {
        name: task
        for name, task in celery_app.tasks.items()
        if task.__module__.startswith("urbanlens.") and ".tests." not in task.__module__
    }


class QueueDefaultTests(SimpleTestCase):
    def test_the_tree_passes_the_check(self) -> None:
        self.assertEqual(check_every_task_has_a_time_limit(), [])

    def test_an_interactive_task_without_limits_gets_the_interactive_default(self) -> None:
        defaults = task_limits.queue_defaults()[Queue.INTERACTIVE]
        task = tasks.deliver_friend_invitation
        self.assertEqual((task.soft_time_limit, task.time_limit), (defaults.soft, defaults.hard))

    def test_no_interactive_task_inherits_the_hour_long_global_limit(self) -> None:
        ceiling = task_limits.queue_ceilings()[Queue.INTERACTIVE]
        interactive = {name: task for name, task in _our_tasks().items() if task.queue == Queue.INTERACTIVE}
        self.assertTrue(interactive)
        for name, task in interactive.items():
            self.assertLessEqual(task.time_limit, ceiling, name)

    def test_a_decorators_own_limits_are_kept(self) -> None:
        self.assertEqual((tasks.fetch_panel_source.soft_time_limit, tasks.fetch_panel_source.time_limit), (110, 130))

    def test_a_bulk_task_keeps_the_global_limits(self) -> None:
        from django.conf import settings

        task = tasks.recompute_fact_confidence
        expected = (settings.CELERY_TASK_SOFT_TIME_LIMIT, settings.CELERY_TASK_TIME_LIMIT)
        self.assertEqual((task.soft_time_limit, task.time_limit), expected)

    def test_every_task_uses_the_project_base_class(self) -> None:
        for name, task in _our_tasks().items():
            self.assertIsInstance(task, task_limits.UrbanLensTask, name)


class LocksOutliveTheirTasksTests(SimpleTestCase):
    """A sweep's overlap lock must not expire while the sweep can still be running."""

    def test_the_safety_sweeps_end_before_their_lock(self) -> None:
        for task in (
            tasks.send_due_checkin_reminders,
            tasks.send_final_checkin_warnings,
            tasks.escalate_overdue_checkins,
            tasks.sweep_due_safety_checkin_archival,
        ):
            self.assertLess(task.time_limit, tasks._CHECKIN_LOCK_TIMEOUT_SECONDS, task.name)

    def test_the_game_stall_sweeps_end_before_their_lock(self) -> None:
        pairs = (
            (tasks.sweep_stalled_spotguessr_sessions, tasks._SPOTGUESSR_STALL_SWEEP_LOCK_TIMEOUT_SECONDS),
            (tasks.sweep_stalled_trivia_sessions, tasks._TRIVIA_STALL_SWEEP_LOCK_TIMEOUT_SECONDS),
            (tasks.sweep_stalled_consensus_sessions, tasks._CONSENSUS_STALL_SWEEP_LOCK_TIMEOUT_SECONDS),
        )
        for task, lock in pairs:
            self.assertLess(task.time_limit, lock, task.name)

    def test_the_hourly_sweeps_end_before_their_lock(self) -> None:
        for task in (
            tasks.run_scheduled_enrichment,
            tasks.run_scheduled_trivia_generation,
            tasks.run_scheduled_trivia_wiki_incorporation,
            tasks.send_account_deletion_reminders,
            tasks.hard_delete_expired_accounts,
        ):
            self.assertLess(task.time_limit, 3300, task.name)

    def test_the_outbox_drain_ends_before_its_lock(self) -> None:
        self.assertLess(tasks.drain_task_outbox.time_limit, tasks._OUTBOX_DRAIN_LOCK_SECONDS)


class TimeLimitCheckTests(SimpleTestCase):
    def _check_with(self, **task_fields):
        fake = SimpleNamespace(__module__="urbanlens.dashboard.fake", **task_fields)
        with mock.patch.object(celery_app, "tasks", {"urbanlens.dashboard.fake.task": fake}):
            return check_every_task_has_a_time_limit()

    def test_a_task_above_its_queue_ceiling_fails(self) -> None:
        errors = self._check_with(queue=Queue.INTERACTIVE, soft_time_limit=2700, time_limit=3600)
        self.assertEqual([error.id for error in errors], ["dashboard.E013"])
        self.assertIn("ceiling", errors[0].msg)

    def test_a_task_with_no_limit_fails(self) -> None:
        errors = self._check_with(queue=Queue.BULK, soft_time_limit=None, time_limit=None)
        self.assertEqual(len(errors), 1)

    def test_an_inverted_pair_fails(self) -> None:
        errors = self._check_with(queue=Queue.BULK, soft_time_limit=100, time_limit=90)
        self.assertEqual(len(errors), 1)

    def test_a_batch_task_is_capped(self) -> None:
        from urbanlens.dashboard.services.core.task_limits import BATCH_CEILING_SECONDS

        errors = self._check_with(queue=Queue.BULK, soft_time_limit=100, time_limit=BATCH_CEILING_SECONDS + 1)
        self.assertEqual(len(errors), 1)


class SoftLimitEscapesBroadHandlersTests(SimpleTestCase):
    """The soft limit reaches the task boundary through ``except Exception`` and is reported as a failure there."""

    def setUp(self) -> None:
        super().setUp()
        original = signal.getsignal(pool.SIG_SOFT_TIMEOUT)
        self.addCleanup(signal.signal, pool.SIG_SOFT_TIMEOUT, original)

    def _task(self, body):
        return celery_app.task(
            name=f"urbanlens.probe.{body.__name__}", base=task_limits.UrbanLensTask, soft_time_limit=1, time_limit=2
        )(body)

    def test_a_swallowing_handler_does_not_keep_the_task_running(self) -> None:
        reached_after_swallow = []

        def swallows_everything():
            try:
                os.kill(os.getpid(), pool.SIG_SOFT_TIMEOUT)
                time.sleep(1)
            except Exception:
                reached_after_swallow.append(True)
            reached_after_swallow.append("carried on")

        signal.signal(pool.SIG_SOFT_TIMEOUT, pool.soft_timeout_sighandler)
        task = self._task(swallows_everything)

        with self.assertRaises(SoftTimeLimitExceeded):
            task()

        self.assertEqual(reached_after_swallow, [])

    def test_code_that_cleans_up_on_a_soft_limit_still_can(self) -> None:
        cleaned = []

        def cleans_up():
            try:
                os.kill(os.getpid(), pool.SIG_SOFT_TIMEOUT)
                time.sleep(1)
            except task_limits.SOFT_TIME_LIMIT_ERRORS:
                cleaned.append(True)
                return "wound down"
            return "finished"

        signal.signal(pool.SIG_SOFT_TIMEOUT, pool.soft_timeout_sighandler)

        self.assertEqual(self._task(cleans_up)(), "wound down")
        self.assertEqual(cleaned, [True])

    def test_a_process_without_billiards_handler_is_left_alone(self) -> None:
        signal.signal(pool.SIG_SOFT_TIMEOUT, signal.SIG_DFL)

        with task_limits.soft_limit_escapes_broad_handlers():
            self.assertIs(signal.getsignal(pool.SIG_SOFT_TIMEOUT), signal.SIG_DFL)

    def test_billiards_handler_is_back_once_the_task_returns(self) -> None:
        seen = []

        def records_the_handler():
            seen.append(signal.getsignal(pool.SIG_SOFT_TIMEOUT))

        signal.signal(pool.SIG_SOFT_TIMEOUT, pool.soft_timeout_sighandler)
        self._task(records_the_handler)()

        self.assertIsNot(seen[0], pool.soft_timeout_sighandler)
        self.assertIs(signal.getsignal(pool.SIG_SOFT_TIMEOUT), pool.soft_timeout_sighandler)


class PreviewLookupDeadlineTests(SimpleTestCase):
    def test_rows_past_the_deadline_are_left_unplaced_without_a_lookup(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleGeocodingGateway, GoogleMapsGateway

        rows = [
            {"stem": "a", "maps_url": "https://maps.example/1"},
            {"stem": "a", "maps_url": "https://maps.example/2"},
        ]
        with mock.patch.object(GoogleGeocodingGateway, "extract_coordinates_from_url") as lookup:
            placed, unavailable = GoogleMapsGateway().resolve_preview_rows(
                rows, mock.Mock(), room=10, deadline=time.monotonic() - 1
            )

        lookup.assert_not_called()
        self.assertEqual((placed, unavailable), ([], 2))
