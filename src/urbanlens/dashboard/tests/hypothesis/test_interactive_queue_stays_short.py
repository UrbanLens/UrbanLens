"""The interactive worker never holds a task that can run for minutes (D13).

Safety escalations, signup mail and trip invites share ``celery-worker``'s four slots. Upstream-bound
fetches with four-minute limits used to share them too: during an integration run, new wikis' enrichment
backed the queue up 34 deep and delayed invitation delivery past a minute. Those fetches run on
``panel_fetch``, whose worker is sized for waiting on upstreams.
"""

from __future__ import annotations

from celery import current_app

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks  # noqa: F401 - registers the tasks
from urbanlens.dashboard.services.core.task_limits import ceiling_for, queue_defaults
from urbanlens.dashboard.services.sandbox.queues import Queue

#: Interactive tasks allowed past the queue's default hard limit, each reviewed: the safety sweeps (D13 keeps
#: them interactive whatever they cost), a model call a person is waiting on (the AI queue's worker holds no
#: application credentials), the import preview (bounded by its own lookup budget), or local work that is
#: slow only on very large maps.
_REVIEWED_LONG_INTERACTIVE = frozenset(
    {
        "urbanlens.dashboard.tasks.escalate_overdue_checkins",
        "urbanlens.dashboard.tasks.send_due_checkin_reminders",
        "urbanlens.dashboard.tasks.send_final_checkin_warnings",
        "urbanlens.dashboard.tasks.finish_import_preview_task",
        "urbanlens.dashboard.tasks.classify_detail_marker",
        "urbanlens.dashboard.tasks.classify_trivia_submission",
        "urbanlens.dashboard.tasks.generate_image_keywords",
        "urbanlens.dashboard.tasks.build_map_document",
    }
)

_UPSTREAM_FETCHES = (
    "enrich_wiki_location",
    "generate_boundaries_for_location",
    "prefetch_location_external_data",
    "refresh_pin_web_search",
    "fetch_panel_source",
    "fetch_recorded_weather",
    "fetch_recorded_weather_at",
    "run_link_extraction",
    "cache_media_item_into_album",
    "cache_media_item_into_wiki",
)


class InteractiveQueueStaysShortTests(SimpleTestCase):
    def _tasks_on(self, queue: str) -> dict:
        return {name: task for name, task in current_app.tasks.items() if getattr(task, "queue", None) == queue}

    def test_no_interactive_task_can_outlast_the_queues_default(self) -> None:
        bound = queue_defaults()[Queue.INTERACTIVE].hard
        long = sorted(
            name
            for name, task in self._tasks_on(Queue.INTERACTIVE).items()
            if (task.time_limit or 0) > bound and name not in _REVIEWED_LONG_INTERACTIVE
        )
        self.assertEqual(
            long,
            [],
            f"these interactive tasks can hold a slot for over {bound}s; an upstream fetch belongs on panel_fetch",
        )

    def test_upstream_fetches_run_on_the_panel_worker(self) -> None:
        for name in _UPSTREAM_FETCHES:
            with self.subTest(name):
                self.assertEqual(current_app.tasks[f"urbanlens.dashboard.tasks.{name}"].queue, Queue.PANEL_FETCH)

    def test_every_panel_task_fits_under_the_panel_ceiling(self) -> None:
        ceiling = ceiling_for(Queue.PANEL_FETCH)
        for name, task in self._tasks_on(Queue.PANEL_FETCH).items():
            with self.subTest(name):
                self.assertLessEqual(task.time_limit or 0, ceiling)
