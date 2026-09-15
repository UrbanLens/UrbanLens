"""Work a model signal queues from inside a batch job goes to the bulk queue (P109).

Importing 1,000 pins left about 2,600 tasks behind it - wiki creation, enrichment, category suggestion and
reputation scoring, a set per pin. D13 classes those interactive, because a person creating one pin is
waiting on them, so an import's worth drained through the same four-slot worker as
``escalate_overdue_checkins``. Queued from inside a batch task they now go to bulk; queued by a person's own
action they keep their interactive queue.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.signals import ensure_wiki_for_pin_location
from urbanlens.dashboard.models.reputation import signals as reputation_signals
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.signals import suggest_and_add_categories
from urbanlens.dashboard.services.core.celery import follow_on_queue
from urbanlens.dashboard.services.pins import confirmed_import, import_preview
from urbanlens.dashboard.services.sandbox.queues import Queue
from urbanlens.dashboard.tasks import (
    enrich_wiki_location,
    ensure_wiki_for_location,
    finish_import_preview_task,
    run_confirmed_pin_import,
    score_reputation_event,
    suggest_wiki_category,
)

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _inside_a_bulk_task(during) -> list[object]:
    """Run *during* inside ``run_confirmed_pin_import``, a task declared on the bulk queue."""
    seen: list[object] = []
    with mock.patch.object(confirmed_import, "run_confirmed_import", side_effect=lambda *_: seen.append(during())):
        run_confirmed_pin_import.apply(args=(1, "job")).get()
    return seen


def _inside_an_interactive_task(during, **delivery) -> list[object]:
    """Run *during* inside ``finish_import_preview_task``, a task declared interactive."""
    seen: list[object] = []
    with mock.patch.object(import_preview, "finish_import_preview", side_effect=lambda *_: seen.append(during())):
        finish_import_preview_task.apply(args=(1, "job"), **delivery).get()
    return seen


class FollowOnQueueTests(SimpleTestCase):
    def test_outside_any_task_the_enqueued_tasks_own_queue_is_kept(self) -> None:
        self.assertIsNone(follow_on_queue())

    def test_inside_a_task_declared_bulk(self) -> None:
        self.assertEqual(_inside_a_bulk_task(follow_on_queue), [Queue.BULK])

    def test_inside_an_interactive_task_on_its_own_queue(self) -> None:
        self.assertEqual(_inside_an_interactive_task(follow_on_queue), [None])

    def test_inside_an_interactive_task_that_was_sent_to_bulk(self) -> None:
        # How the chain carries: ensure_wiki_for_location is declared interactive, and routed to bulk per call.
        self.assertEqual(_inside_an_interactive_task(follow_on_queue, routing_key=Queue.BULK), [Queue.BULK])

    def test_inside_the_data_import_queue(self) -> None:
        self.assertEqual(_inside_an_interactive_task(follow_on_queue, routing_key=Queue.SANDBOX_BATCH), [Queue.BULK])

    def test_inside_a_photo_upload_the_work_stays_interactive(self) -> None:
        self.assertEqual(_inside_an_interactive_task(follow_on_queue, routing_key=Queue.SANDBOX), [None])


class _Profile:
    community_enabled = True


class _Pin:
    location_id = 55
    parent_pin_id = None
    profile_id = 7
    profile = _Profile()


class _Wiki:
    pk = 20


class SignalsQueueFollowOnWorkTests(SimpleTestCase):
    def _enqueued(self, signal_module: str, fire, *, in_bulk: bool) -> mock.Mock:
        """Fire a signal, then run its on-commit callbacks after the surrounding task has returned."""
        callbacks: list = []
        with (
            mock.patch(f"{signal_module}.transaction.on_commit", side_effect=callbacks.append),
            mock.patch(ENQUEUE) as enqueue,
        ):
            if in_bulk:
                _inside_a_bulk_task(fire)
            else:
                fire()
            self.assertEqual(len(callbacks), 1, "the premise failed: the signal queued nothing")
            callbacks[0]()
        return enqueue

    def _fire_pin(self) -> None:
        ensure_wiki_for_pin_location(sender=object, instance=_Pin(), created=True)

    def test_an_imported_pins_wiki_is_created_on_bulk(self) -> None:
        enqueue = self._enqueued("urbanlens.dashboard.models.pin.signals", self._fire_pin, in_bulk=True)

        enqueue.assert_called_once_with(ensure_wiki_for_location, 55, queue=Queue.BULK)

    def test_a_pin_someone_adds_keeps_the_interactive_queue(self) -> None:
        enqueue = self._enqueued("urbanlens.dashboard.models.pin.signals", self._fire_pin, in_bulk=False)

        enqueue.assert_called_once_with(ensure_wiki_for_location, 55, queue=None)

    def test_an_imported_wikis_categories_are_suggested_on_bulk(self) -> None:
        def fire() -> None:
            suggest_and_add_categories(sender=object, instance=_Wiki(), created=True)

        enqueue = self._enqueued("urbanlens.dashboard.models.wiki.signals", fire, in_bulk=True)

        enqueue.assert_called_once_with(suggest_wiki_category, _Wiki.pk, queue=Queue.BULK)

    def test_an_imported_pins_reputation_is_scored_on_bulk(self) -> None:
        (subscription,) = [sub for sub in reputation_signals._SUBSCRIPTIONS if sub.rule_key == "pin_created"]
        handler = reputation_signals._make_handler(subscription)

        def fire() -> None:
            handler(sender=object, instance=_Pin(), created=True, raw=False)

        with mock.patch("urbanlens.dashboard.services.reputation.scoring.record_event", return_value=mock.Mock(pk=31)):
            enqueue = self._enqueued("urbanlens.dashboard.models.reputation.signals", fire, in_bulk=True)

        enqueue.assert_called_once_with(score_reputation_event, 31, queue=Queue.BULK)


class WikiEnrichmentFollowsTheWikiTests(SimpleTestCase):
    def _ensure(self, **delivery) -> mock.Mock:
        wiki = mock.Mock(pk=9)
        with (
            mock.patch.object(Location.objects, "filter") as locations,
            mock.patch.object(Wiki.objects, "get_or_create_for_location", return_value=(wiki, True)),
            mock.patch("urbanlens.dashboard.services.wiki.wiki_seed.seed_wiki_article_from_wikipedia"),
            mock.patch(ENQUEUE) as enqueue,
        ):
            locations.return_value.first.return_value = mock.Mock(pk=55)
            ensure_wiki_for_location.apply(args=(55,), **delivery).get()
        return enqueue

    def test_a_wiki_created_on_bulk_is_enriched_on_bulk(self) -> None:
        self._ensure(routing_key=Queue.BULK).assert_called_once_with(enrich_wiki_location, 9, queue=Queue.BULK)

    def test_a_wiki_created_on_the_interactive_queue_is_enriched_there(self) -> None:
        self._ensure(routing_key=Queue.INTERACTIVE).assert_called_once_with(enrich_wiki_location, 9, queue=None)
