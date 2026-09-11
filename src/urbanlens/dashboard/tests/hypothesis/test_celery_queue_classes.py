"""One account's big job must not delay everyone's small ones.

D13 measured it: 87 of 96 task definitions declared no queue, so all of them
landed on `celery` and were drained by one worker at `--concurrency=4`. An Immich
library sweep holds one of those four slots for the better part of an hour, and
`escalate_overdue_checkins` - the task whose whole job is telling somebody that a
person is overdue - queues behind it.

The split is the same one `sandbox`/`sandbox_batch` already makes for media, for
the same stated reason, applied to the queue everything else shares.

Three things have to hold together, and each is worthless without the others:

* every task the app defines names a class, enforced at startup rather than by
  review, because the failure mode is silence - a task with no `queue=` keeps
  working, on the wrong pool;
* a container drains every class that is named, because a queue nothing consumes
  loses work in a way that looks exactly like a slow worker;
* the safety check-in tasks are interactive, whatever the beat-driven rule would
  otherwise say about them.
"""

from __future__ import annotations

import pathlib
from typing import Any

from django.core.checks import Error
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.checks import check_every_task_declares_a_queue
from urbanlens.dashboard.services.sandbox.queues import Queue

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"

#: Beat-driven, and interactive anyway: each one exists to tell a person that
#: somebody is overdue. D13 calls this placement out as the one most worth a
#: second opinion, which is why it is asserted rather than left to the table.
_SAFETY_TASKS = (
    "urbanlens.dashboard.tasks.escalate_overdue_checkins",
    "urbanlens.dashboard.tasks.send_due_checkin_reminders",
    "urbanlens.dashboard.tasks.send_final_checkin_warnings",
)

#: A task whose cost is set by how much one account owns. Enough of the table to
#: catch a wholesale reclassification; not the whole table, which lives in D13.
_BULK_TASKS = (
    "urbanlens.dashboard.tasks.sweep_immich_library_locations",
    "urbanlens.dashboard.tasks.run_user_data_export",
    "urbanlens.dashboard.tasks.process_device_scan_upload",
)


def _app_tasks() -> dict[str, Any]:
    """Every task this codebase defines outside its own test suite, by name.

    Returns:
        The registry entries whose implementation lives in `urbanlens` and is
        not a probe task defined by a test - the same scope the startup check
        applies, so the two cannot disagree about what they are measuring.
    """
    from urbanlens.UrbanLens.celery import app as celery_app

    return {
        name: task
        for name, task in celery_app.tasks.items()
        if (module := getattr(task, "__module__", "")).startswith("urbanlens.") and ".tests." not in module
    }


def _compose_services() -> dict[str, Any]:
    """`docker-compose.yml`, parsed.

    Parsed rather than grepped: the services are nested, several of them carry a
    `-Q`, and a regex over the whole file cannot say which one a match belongs
    to - which is how the first version of this file read the bulk worker's
    queues as the interactive worker's.

    Returns:
        The `services` mapping.
    """
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))["services"]


def _queues_of(service: dict[str, Any]) -> set[str]:
    """The queues one service's command drains.

    Args:
        service: One entry from the compose `services` mapping.

    Returns:
        The names following `-Q`, split on commas. Empty for a service that
        passes none, which for a Celery worker means the default queue.
    """
    command = service.get("command") or []
    if not isinstance(command, list) or "-Q" not in command:
        return set()
    return {part.strip() for part in command[command.index("-Q") + 1].split(",")}


def _worker_queues() -> set[str]:
    """Every queue some compose service drains.

    Returns:
        The union across all services.
    """
    return {queue for service in _compose_services().values() for queue in _queues_of(service)}


class TheRegistryIsRealTests(SimpleTestCase):
    """Guards every other test here: an empty registry would pass them all."""

    def test_the_tasks_are_discovered(self) -> None:
        tasks = _app_tasks()

        self.assertGreater(len(tasks), 80, f"only {len(tasks)} tasks discovered; the registry is not populated")
        self.assertIn("urbanlens.dashboard.tasks.sweep_immich_library_locations", tasks)
        self.assertIn("urbanlens.dashboard.services.ai.tasks.run_assistant_turn_task", tasks)

    def test_the_compose_file_really_declares_queues(self) -> None:
        self.assertIn(Queue.SANDBOX, _worker_queues(), "no -Q was parsed out of docker-compose.yml")


class EveryTaskNamesItsClassTests(SimpleTestCase):
    """The declaration has to be on the task, not at the call sites."""

    def test_no_task_is_left_on_the_default_queue(self) -> None:
        unrouted = sorted(name for name, task in _app_tasks().items() if not getattr(task, "queue", None))

        self.assertEqual(unrouted, [], f"{len(unrouted)} task(s) declare no queue: {unrouted}")

    def test_the_startup_check_agrees(self) -> None:
        self.assertEqual([], check_every_task_declares_a_queue())

    def test_the_check_reports_an_unrouted_task(self) -> None:
        """Otherwise the check is a function that returns an empty list.

        The probe claims `dashboard.tasks` as its module because that is what it
        is standing in for. Registering it as what it really is - a function in a
        test - would land it in the exemption the check gives test modules, and
        this would then pass against a check that reports nothing at all.
        """
        from urbanlens.UrbanLens.celery import app as celery_app

        name = "urbanlens.dashboard.tasks.a_task_that_forgot"

        @celery_app.task(name=name)
        def _forgot() -> None:
            """A task defined without a queue."""

        celery_app.tasks[name].__module__ = "urbanlens.dashboard.tasks"
        try:
            errors = check_every_task_declares_a_queue()
        finally:
            celery_app.tasks.pop(name, None)

        self.assertTrue(errors, "the check passed a task with no queue")
        self.assertIsInstance(errors[0], Error)
        self.assertIn("a_task_that_forgot", str(errors[0]))


class EveryQueueHasAWorkerTests(SimpleTestCase):
    """A queue nothing drains loses work while looking like a slow worker."""

    def test_each_declared_queue_is_drained_by_a_container(self) -> None:
        drained = _worker_queues()
        declared = {str(task.queue) for task in _app_tasks().values() if getattr(task, "queue", None)}

        self.assertEqual(
            sorted(declared - drained),
            [],
            f"tasks are routed to queues no compose service drains: {sorted(declared - drained)}",
        )

    def test_the_default_queue_is_still_drained(self) -> None:
        """The safety net for a task whose declaration is added later."""
        self.assertIn(
            Queue.DEFAULT,
            _worker_queues(),
            "nothing drains 'celery', so a task that forgets its queue would vanish rather than run slowly",
        )

    def test_the_interactive_pool_drains_nothing_slow(self) -> None:
        """The whole invariant: the pool a person waits on carries only its own class."""
        queues = _queues_of(_compose_services()["celery-worker"])

        self.assertEqual(
            queues,
            {str(Queue.INTERACTIVE)},
            f"celery-worker drains {sorted(queues)}; anything beyond the interactive class can delay it",
        )

    def test_a_second_pool_drains_the_rest(self) -> None:
        """Named explicitly, so deleting the bulk worker fails here rather than
        silently moving every account-sized job back onto the interactive pool."""
        queues = _queues_of(_compose_services()["celery-worker-bulk"])

        self.assertEqual(queues, {str(Queue.BULK), str(Queue.MAINTENANCE), str(Queue.DEFAULT)})


class SafetyOverridesTheBeatRuleTests(SimpleTestCase):
    """Beat-driven would make these maintenance. What they do makes them not."""

    def test_the_checkin_escalation_path_is_interactive(self) -> None:
        tasks = _app_tasks()
        for name in _SAFETY_TASKS:
            self.assertIn(name, tasks)
            self.assertEqual(
                str(tasks[name].queue),
                str(Queue.INTERACTIVE),
                f"{name} is on {tasks[name].queue}; a photo import must never be able to delay it",
            )

    def test_the_account_sized_jobs_are_bulk(self) -> None:
        tasks = _app_tasks()
        for name in _BULK_TASKS:
            self.assertIn(name, tasks)
            self.assertEqual(str(tasks[name].queue), str(Queue.BULK), f"{name} is on {tasks[name].queue}")
