"""A delivery stays unacknowledged while its countdown runs and its task runs, and the broker must outwait both (P290).

RabbitMQ's default consumer timeout is 30 minutes. Past it the broker closes the worker's channel, and the worker
exits with "Unrecoverable error: PreconditionFailed", taking every task it was running with it. On dev the interactive
worker did this every 31 minutes for hours while a boundary retry's two-hour countdown waited.
"""

from __future__ import annotations

import pathlib
import re
from unittest import mock

from django.conf import settings
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.services.core import celery as celery_service
from urbanlens.dashboard.services.core.celery import LONGEST_COUNTDOWN_SECONDS, safely_enqueue_task
from urbanlens.dashboard.services.import_export.export import EXPORT_TTL_SECONDS
from urbanlens.dashboard.services.import_export.import_data import IMPORT_TTL_SECONDS
from urbanlens.dashboard.services.locations import boundaries
from urbanlens.dashboard.services.media.storage_errors import IMPORT_STORAGE_WAITS, storage_retry_countdown
from urbanlens.dashboard.services.visits.safety import ARCHIVE_VIEWER_GRACE_PERIOD

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

#: A delivery is held for its countdown, then for its task's run.
_LONGEST_HOLD_SECONDS = LONGEST_COUNTDOWN_SECONDS + settings.CELERY_TASK_TIME_LIMIT


def _compose_consumer_timeout_seconds() -> float:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    arguments = compose["services"]["rabbitmq"]["environment"].get("RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS", "")
    found = re.search(r"-rabbit\s+consumer_timeout\s+(\d+)", arguments)
    return int(found.group(1)) / 1000 if found else 30 * 60


class TheBrokerOutwaitsTheLongestHoldTests(SimpleTestCase):
    def test_rabbitmqs_consumer_timeout(self) -> None:
        self.assertGreater(_compose_consumer_timeout_seconds(), _LONGEST_HOLD_SECONDS)

    def test_the_redis_fallbacks_visibility_timeout(self) -> None:
        self.assertGreater(settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"], _LONGEST_HOLD_SECONDS)

    def test_every_scheduled_countdown_is_within_the_bound(self) -> None:
        for name, countdown in {
            "deferred pin resolution": max(tasks._DEFERRED_RETRY_SCHEDULE),
            "deferred boundary": boundaries.DEFERRED_RETRY_BASE_SECONDS * 2 ** (boundaries.MAX_DEFERRED_RETRIES - 1),
            "import cleanup": IMPORT_TTL_SECONDS,
            "export cleanup": EXPORT_TTL_SECONDS,
            "check-in archive": ARCHIVE_VIEWER_GRACE_PERIOD.total_seconds(),
            "storage wait": storage_retry_countdown(IMPORT_STORAGE_WAITS),
        }.items():
            with self.subTest(name):
                self.assertLessEqual(countdown, LONGEST_COUNTDOWN_SECONDS)


class AnOverlongCountdownIsShortenedTests(SimpleTestCase):
    def test_a_countdown_past_the_bound_runs_at_the_bound(self) -> None:
        task = mock.Mock()

        safely_enqueue_task(task, 1, countdown=LONGEST_COUNTDOWN_SECONDS + 3600)

        self.assertEqual(task.apply_async.call_args.kwargs["countdown"], LONGEST_COUNTDOWN_SECONDS)

    def test_a_refused_enqueue_keeps_the_shortened_countdown(self) -> None:
        task = mock.Mock()
        task.apply_async.side_effect = celery_service.BROKER_ERRORS[0]("down")

        with mock.patch("urbanlens.dashboard.services.core.task_outbox.record_refused_enqueue") as record:
            safely_enqueue_task(task, 1, countdown=LONGEST_COUNTDOWN_SECONDS * 2)

        self.assertEqual(record.call_args.kwargs["countdown"], LONGEST_COUNTDOWN_SECONDS)

    def test_a_countdown_within_the_bound_is_kept(self) -> None:
        task = mock.Mock()

        safely_enqueue_task(task, 1, countdown=LONGEST_COUNTDOWN_SECONDS)

        self.assertEqual(task.apply_async.call_args.kwargs["countdown"], LONGEST_COUNTDOWN_SECONDS)

    def test_a_provider_asking_for_a_day_is_asked_again_within_the_bound(self) -> None:
        """A boundary provider's own wait used to become the countdown unbounded."""
        location = mock.Mock(pk=7)

        with mock.patch.object(tasks.generate_boundaries_for_location, "apply_async") as apply_async:
            boundaries._schedule_deferred_retry(location, 86_400, attempt=0, force=False)

        self.assertEqual(apply_async.call_args.kwargs["countdown"], LONGEST_COUNTDOWN_SECONDS)
