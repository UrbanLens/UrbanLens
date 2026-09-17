"""Tests for the chunk-shaped batch task siblings tasks.py exposes for P109's per-pin fan-out.

Each batch task below calls its per-item sibling *directly* (as a plain call, not via ``.delay()``/
``.apply_async()``), so a transient failure there can't schedule its own delayed retry: Celery's
``Task.retry`` immediately re-raises instead of scheduling one whenever ``request.called_directly``
is true, which it always is for a task invoked by bare function call rather than reached through the
broker (``celery/app/task.py``'s ``Context.called_directly`` defaults to ``True``, and only the real
worker/apply trace path sets it ``False``). These tests pin down the two failure classes a chunk pass
must tell apart: a transient ``OSError`` must re-raise out of the chunk task so *its own*
``autoretry_for=(OSError,)`` retries the whole chunk, while any other exception must stay isolated to
its one item so the rest of the chunk still runs.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks


class EnsureWikisForLocationsChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(location_id: int) -> int:
            if location_id == 2:
                raise ValueError("boom")
            return location_id * 10

        with mock.patch.object(tasks, "ensure_wiki_for_location", side_effect=fake):
            result = tasks.ensure_wikis_for_locations([1, 2, 3])

        self.assertEqual(result, [10, 30])

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "ensure_wiki_for_location", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.ensure_wikis_for_locations([1, 2, 3])


class EnrichWikiLocationsChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(wiki_id: int) -> bool:
            if wiki_id == 2:
                raise ValueError("boom")
            return True

        with mock.patch.object(tasks, "enrich_wiki_location", side_effect=fake):
            result = tasks.enrich_wiki_locations([1, 2, 3])

        self.assertEqual(result, {1: True, 2: False, 3: True})

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "enrich_wiki_location", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.enrich_wiki_locations([1, 2, 3])


class SuggestWikiCategoriesChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(wiki_id: int) -> list[str]:
            if wiki_id == 2:
                raise ValueError("boom")
            return ["tag"]

        with mock.patch.object(tasks, "suggest_wiki_category", side_effect=fake):
            result = tasks.suggest_wiki_categories([1, 2, 3])

        self.assertEqual(result, {1: ["tag"], 2: [], 3: ["tag"]})

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "suggest_wiki_category", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.suggest_wiki_categories([1, 2, 3])


class SuggestPinCategoriesChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(pin_id: int) -> list[str]:
            if pin_id == 2:
                raise ValueError("boom")
            return ["tag"]

        with mock.patch.object(tasks, "suggest_pin_category", side_effect=fake):
            result = tasks.suggest_pin_categories([1, 2, 3])

        self.assertEqual(result, {1: ["tag"], 2: [], 3: ["tag"]})

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "suggest_pin_category", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.suggest_pin_categories([1, 2, 3])


class ScoreReputationEventsChunkTests(SimpleTestCase):
    """score_reputation_events calls score_event/recompute_total (plain functions), never the
    score_reputation_event *task* object, so it never hits the called_directly trap the other five
    chunk tasks above must guard against - covered here as the negative case."""

    def test_calls_the_plain_scoring_function_not_the_task_object(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.models.reputation.model.ReputationEvent.objects") as manager,
            mock.patch("urbanlens.dashboard.services.reputation.scoring.score_event"),
            mock.patch("urbanlens.dashboard.services.reputation.scoring.recompute_total"),
            mock.patch("urbanlens.dashboard.tasks.score_reputation_event") as score_reputation_event_task,
        ):
            manager.filter.return_value = []
            tasks.score_reputation_events([1, 2, 3])

        score_reputation_event_task.assert_not_called()


class ArchivePinLinksToWaybackChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(link_model: str, link_id: int) -> bool:
            if link_id == 2:
                raise ValueError("boom")
            return True

        with mock.patch.object(tasks, "_archive_link_to_wayback", side_effect=fake):
            result = tasks.archive_pin_links_to_wayback([1, 2, 3])

        self.assertEqual(result, {1: True, 2: False, 3: True})

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "_archive_link_to_wayback", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.archive_pin_links_to_wayback([1, 2, 3])

    def test_archives_under_the_pin_link_model(self) -> None:
        with mock.patch.object(tasks, "_archive_link_to_wayback", return_value=True) as archive:
            tasks.archive_pin_links_to_wayback([1])

        archive.assert_called_once_with("PinLink", 1)


class ArchiveWikiLinksToWaybackChunkTests(SimpleTestCase):
    def test_a_permanent_failure_is_isolated_and_the_rest_of_the_chunk_still_runs(self) -> None:
        def fake(link_model: str, link_id: int) -> bool:
            if link_id == 2:
                raise ValueError("boom")
            return True

        with mock.patch.object(tasks, "_archive_link_to_wayback", side_effect=fake):
            result = tasks.archive_wiki_links_to_wayback([1, 2, 3])

        self.assertEqual(result, {1: True, 2: False, 3: True})

    def test_a_transient_oserror_reraises_to_retry_the_whole_chunk_not_swallowed(self) -> None:
        with (
            mock.patch.object(tasks, "_archive_link_to_wayback", side_effect=OSError("transient")),
            self.assertRaises(OSError),
        ):
            tasks.archive_wiki_links_to_wayback([1, 2, 3])

    def test_archives_under_the_wiki_link_model(self) -> None:
        with mock.patch.object(tasks, "_archive_link_to_wayback", return_value=True) as archive:
            tasks.archive_wiki_links_to_wayback([1])

        archive.assert_called_once_with("WikiLink", 1)


class ArchiveLinkToWaybackSingleItemShimTests(SimpleTestCase):
    """The single-id shims exist only to fit enqueue_follow_on's one-argument contract - they must
    not reintroduce the two-model dispatch archive_link_to_wayback itself still owns."""

    def test_pin_link_shim_fixes_the_model_to_pin_link(self) -> None:
        with mock.patch.object(tasks, "_archive_link_to_wayback", return_value=True) as archive:
            result = tasks.archive_pin_link_to_wayback(7)

        archive.assert_called_once_with("PinLink", 7)
        self.assertTrue(result)

    def test_wiki_link_shim_fixes_the_model_to_wiki_link(self) -> None:
        with mock.patch.object(tasks, "_archive_link_to_wayback", return_value=True) as archive:
            result = tasks.archive_wiki_link_to_wayback(9)

        archive.assert_called_once_with("WikiLink", 9)
        self.assertTrue(result)
