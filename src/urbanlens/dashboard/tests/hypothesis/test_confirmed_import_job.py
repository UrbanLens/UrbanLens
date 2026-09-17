"""A confirmed import is a job: stored, queued once per account, polled, and cleaned up however it ends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from typing import Any
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.import_export.import_data import IMPORT_TTL_SECONDS
from urbanlens.dashboard.services.import_export.vestigial_assets import cleanup_vestigial_assets
from urbanlens.dashboard.services.pins import confirmed_import
from urbanlens.dashboard.services.pins.confirmed_import import (
    PAYLOAD_FILENAME,
    guard_key,
    job_dir,
    run_confirmed_import,
)
from urbanlens.dashboard.tasks import run_confirmed_pin_import

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
EVENTS = "urbanlens.dashboard.services.apis.locations.google.maps.GoogleMapsGateway.iter_confirmed_import_events"


def _lists(count: int) -> list[dict[str, Any]]:
    pins = [
        {
            "name": f"Imported {index}",
            "lat": 30.0 + index * 0.5,
            "lng": -120.0 + index * 0.5,
            "description": "",
            "cid": None,
            "maps_url": "",
            "label_ids": [],
        }
        for index in range(count)
    ]
    return [{"stem": "probe", "create_category": False, "label_ids": [], "pins": pins}]


class ConfirmedImportJobTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.addCleanup(single_flight.release, guard_key(self.profile.pk))

    def _post(self, lists: Any) -> Any:
        body = json.dumps({"lists": lists, "auto_tag": False})
        return self.client.post(reverse("pin.import.confirmed"), data=body, content_type="application/json")

    def _queued(self, count: int = 3) -> tuple[Any, mock.Mock]:
        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            response = self._post(_lists(count))
        return response, enqueue

    def _pins(self) -> int:
        return Pin.objects.filter(profile=self.profile).count()

    def test_an_accepted_import_is_queued_by_id_with_its_selection_stored(self) -> None:
        response, enqueue = self._queued()

        self.assertEqual(response.status_code, 202, response.content)
        job_id = response.json()["job_id"]
        enqueue.assert_called_once_with(run_confirmed_pin_import, self.profile.pk, job_id)
        with open(os.path.join(job_dir(job_id), PAYLOAD_FILENAME), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["lists"], _lists(3))
        self.assertEqual(self._pins(), 0)

    def test_a_second_import_waits_for_the_first(self) -> None:
        first, _ = self._queued()
        second, enqueue = self._queued()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["job_id"], first.json()["job_id"])
        enqueue.assert_not_called()

    def test_another_account_is_not_held_up_by_it(self) -> None:
        self._queued()
        self.client.force_login(baker.make(User))

        response, enqueue = self._queued()

        self.assertEqual(response.status_code, 202, response.content)
        enqueue.assert_called_once()

    def test_an_unavailable_queue_leaves_nothing_behind(self) -> None:
        root = os.path.dirname(job_dir("probe"))
        before = set(os.listdir(root)) if os.path.isdir(root) else set()

        with mock.patch(ENQUEUE, return_value=None):
            response = self._post(_lists(3))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(set(os.listdir(root)) if os.path.isdir(root) else set(), before)
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_malformed_selection_is_refused_before_anything_is_stored(self) -> None:
        for lists in ({"pins": []}, [], [{"pins": "not a list"}], [{"pins": ["not a pin"]}], [{"pins": []}]):
            with self.subTest(lists=lists), mock.patch(ENQUEUE) as enqueue:
                response = self._post(lists)

                self.assertEqual(response.status_code, 400)
                enqueue.assert_not_called()
                self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_the_task_imports_the_stored_selection_and_cleans_up(self) -> None:
        response, _ = self._queued(3)
        job = response.json()

        run_confirmed_import(self.profile.pk, job["job_id"])

        self.assertEqual(self._pins(), 3)
        status = self.client.get(job["status_url"]).json()
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["result"]["created"], 3)
        self.assertFalse(os.path.exists(job_dir(job["job_id"])))
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_an_import_cancelled_before_its_worker_starts_creates_nothing(self) -> None:
        """Closing the dialog while the job waits in the queue is the ordinary way to cancel."""
        response, _ = self._queued(3)
        job = response.json()

        self.assertEqual(self.client.post(job["cancel_url"]).status_code, 202)
        run_confirmed_import(self.profile.pk, job["job_id"])

        self.assertEqual(self._pins(), 0)
        self.assertEqual(self.client.get(job["status_url"]).json()["status"], "cancelled")
        self.assertFalse(os.path.exists(job_dir(job["job_id"])))
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_cancel_stops_the_import_at_its_next_progress_write(self) -> None:
        response, _ = self._queued(3)
        job = response.json()
        looks: list[str] = []

        def cancelled_once_it_has_started(key: str, **_kwargs: object) -> bool:
            looks.append(key)
            return len(looks) > 1

        with (
            mock.patch.object(confirmed_import, "PROGRESS_EVERY", 1),
            mock.patch.object(confirmed_import, "get_or_none", side_effect=cancelled_once_it_has_started),
        ):
            run_confirmed_import(self.profile.pk, job["job_id"])

        self.assertEqual(self.client.get(job["status_url"]).json()["status"], "cancelled")
        self.assertEqual(self._pins(), 1)
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_someone_else_can_neither_read_nor_cancel_it(self) -> None:
        response, _ = self._queued(3)
        job = response.json()
        self.client.force_login(baker.make(User))

        self.assertEqual(self.client.get(job["status_url"]).status_code, 404)
        self.assertEqual(self.client.post(job["cancel_url"]).status_code, 404)
        with mock.patch.object(confirmed_import, "PROGRESS_EVERY", 1):
            run_confirmed_import(self.profile.pk, job["job_id"])

        self.assertEqual(self._pins(), 3)

    def test_an_import_that_runs_out_of_time_reports_how_far_it_got(self) -> None:
        response, _ = self._queued(3)
        job = response.json()
        first = {"type": "progress", "current": 1, "total": 3, "percent": 33, "created": 1}
        first |= {"exists": 0, "skipped": 0, "outcome": "created", "name": "Imported 0"}

        def events(*_args: Any, **_kwargs: Any) -> Any:
            yield {"type": "start", "total": 3}
            yield first
            raise SoftTimeLimitExceeded

        with mock.patch(EVENTS, side_effect=events), mock.patch.object(confirmed_import, "PROGRESS_EVERY", 1):
            run_confirmed_import(self.profile.pk, job["job_id"])

        status = self.client.get(job["status_url"]).json()
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["result"]["current"], 1)
        self.assertFalse(os.path.exists(job_dir(job["job_id"])))
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_task_that_finishes_before_the_request_does_still_frees_the_account(self) -> None:
        """Run inline, the task ends inside the enqueue - before the view returns."""
        with tasks_run_inline(run_confirmed_pin_import):
            response = self._post(_lists(2))

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self._pins(), 2)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["status"], "done")
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_selection_waiting_for_a_worker_outlives_the_data_import_sweep(self) -> None:
        """Two full-size imports hold both bulk slots for over an hour, so a third can wait that long."""
        response, _ = self._queued()
        payload = os.path.join(job_dir(response.json()["job_id"]), PAYLOAD_FILENAME)
        stored = datetime.fromtimestamp(os.stat(os.path.dirname(payload)).st_mtime, tz=UTC)

        cleanup_vestigial_assets(now=stored + timedelta(seconds=IMPORT_TTL_SECONDS + 60))
        self.assertTrue(os.path.exists(payload), "the sweep deleted a selection still waiting for its worker")

        cleanup_vestigial_assets(now=stored + timedelta(seconds=confirmed_import.GUARD_TTL_SECONDS + 60))
        self.assertFalse(os.path.exists(payload), "an abandoned selection was never swept")

    def test_its_status_outlives_the_longest_it_can_wait(self) -> None:
        with mock.patch.object(cache, "set", wraps=cache.set) as cache_set:
            response, _ = self._queued()

        job_id = response.json()["job_id"]
        timeouts = [call.kwargs.get("timeout") for call in cache_set.call_args_list if job_id in str(call.args[0])]
        self.assertTrue(timeouts, "no status was written for the job")
        self.assertTrue(all(timeout >= confirmed_import.TIME_LIMIT_SECONDS for timeout in timeouts), timeouts)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ConfirmedImportFollowOnBatchingTests(TransactionTestCase):
    """A real import's per-pin follow-on work (wiki creation, category suggestion, reputation
    scoring) coalesces into bounded chunks rather than one broker task per pin (P109).

    ``run_confirmed_import`` wraps its per-pin loop in ``batching_follow_on_work()``, so every pin
    created inside it - including the reputation event ``Pin``'s own post_save signal records - has
    its follow-on work buffered and flushed as chunk-shaped batch tasks. Follow-on enqueues are
    observed by patching ``bulk_followup.safely_enqueue_task`` directly rather than running the
    downstream batch tasks (which would hit Wiki creation, AI tagging and reputation scoring for
    real) - this suite is about how many broker messages an import creates and what they carry, not
    what those messages do once picked up.

    A ``TransactionTestCase``, not the project's usual ``TestCase``, on purpose: ``run_confirmed_import``
    never wraps its per-pin loop in an explicit transaction, so in production each pin's row commits
    (and its post_save ``transaction.on_commit`` callback fires) the moment it is created - while the
    surrounding ``batching_follow_on_work()`` collector is still open. The project ``TestCase`` wraps a
    whole test in one outer transaction that never really commits, so on_commit callbacks would only
    ever run (via ``captureOnCommitCallbacks``) after the whole import - and therefore after the
    collector - had already closed, batching only the one family (category suggestion) that isn't
    deferred through a signal and silently un-batching the other two. A real, uncommitted-nothing
    transaction is what makes this suite test what production actually does.
    """

    FOLLOW_ON_ENQUEUE = "urbanlens.dashboard.services.core.bulk_followup.safely_enqueue_task"

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.addCleanup(single_flight.release, guard_key(self.profile.pk))

    def _run_import(self, count: int) -> mock.Mock:
        """Store and run a confirmed import of *count* pins; return the observed follow-on enqueues."""
        with mock.patch(ENQUEUE, return_value=mock.Mock()):
            started = confirmed_import.start_confirmed_import(self.profile, _lists(count), auto_tag=True)
        with mock.patch(self.FOLLOW_ON_ENQUEUE) as enqueue:
            run_confirmed_import(self.profile.pk, started.job_id)
        return enqueue

    def _pins(self) -> int:
        return Pin.objects.filter(profile=self.profile).count()

    def _pin_ids(self) -> list[int]:
        return list(Pin.objects.filter(profile=self.profile).order_by("pk").values_list("pk", flat=True))

    def test_before_any_import_the_profile_has_no_follow_on_work_pending(self) -> None:
        """Baseline: an untouched profile has created nothing for the fan-out to ever act on."""
        from urbanlens.dashboard.models.reputation.model import ReputationEvent

        self.assertEqual(self._pins(), 0)
        self.assertEqual(ReputationEvent.objects.filter(profile=self.profile).count(), 0)

    def test_a_small_import_still_queues_its_follow_on_work(self) -> None:
        """A chunk of one still flushes - the mechanism itself must not silently swallow small imports."""
        enqueue = self._run_import(3)

        self.assertTrue(enqueue.called, "a 3-pin import queued no follow-on work at all")

    def test_a_large_import_bounds_its_follow_on_enqueue_count_not_one_task_per_pin(self) -> None:
        """The headline claim: a 120-pin import's follow-on enqueues track pins/chunk_size, not pins."""
        from urbanlens.dashboard.services.core.bulk_followup import DEFAULT_CHUNK_SIZE
        from urbanlens.dashboard.tasks import (
            ensure_wikis_for_locations,
            score_reputation_events,
            suggest_pin_categories,
        )

        pin_count = 120
        expected_chunks_per_family = -(-pin_count // DEFAULT_CHUNK_SIZE)  # ceil

        enqueue = self._run_import(pin_count)

        self.assertEqual(self._pins(), pin_count)
        by_task: dict[object, list[list[int]]] = {}
        for call in enqueue.call_args_list:
            task, ids = call.args
            by_task.setdefault(task, []).append(ids)

        for task in (ensure_wikis_for_locations, suggest_pin_categories, score_reputation_events):
            chunks = by_task.get(task, [])
            self.assertLessEqual(
                len(chunks),
                expected_chunks_per_family,
                f"{task.name} was enqueued {len(chunks)} times for {pin_count} pins - expected at most {expected_chunks_per_family} chunked calls, not one per pin",
            )
            self.assertGreater(len(chunks), 0, f"{task.name} was never enqueued for a {pin_count}-pin import")
            for chunk in chunks:
                self.assertLessEqual(
                    len(chunk), DEFAULT_CHUNK_SIZE, f"{task.name} received an oversized chunk: {chunk}"
                )

    def test_a_large_imports_follow_on_ids_are_not_lost_or_duplicated(self) -> None:
        """Every pin's follow-on work must reach a batch task exactly once - none dropped, none doubled."""
        from urbanlens.dashboard.models.reputation.model import ReputationEvent
        from urbanlens.dashboard.tasks import (
            ensure_wikis_for_locations,
            score_reputation_events,
            suggest_pin_categories,
        )

        pin_count = 120
        enqueue = self._run_import(pin_count)

        flushed: dict[object, list[int]] = {}
        for call in enqueue.call_args_list:
            task, ids = call.args
            flushed.setdefault(task, []).extend(ids)

        pin_ids = self._pin_ids()
        self.assertEqual(len(pin_ids), pin_count)
        self.assertEqual(
            sorted(flushed.get(suggest_pin_categories, [])),
            pin_ids,
            "every created pin should have its category suggestion queued exactly once",
        )

        location_ids = sorted(Pin.objects.filter(pk__in=pin_ids).values_list("location_id", flat=True))
        self.assertEqual(
            sorted(flushed.get(ensure_wikis_for_locations, [])),
            location_ids,
            "every pin's location should have wiki creation queued exactly once",
        )

        event_ids = sorted(
            ReputationEvent.objects.filter(profile=self.profile, rule_key="pin_created").values_list("pk", flat=True)
        )
        self.assertEqual(
            len(event_ids), pin_count, "the premise failed: not every pin recorded a pin_created reputation event"
        )
        self.assertEqual(
            sorted(flushed.get(score_reputation_events, [])),
            event_ids,
            "every pin's reputation event should be queued for scoring exactly once",
        )

    def test_after_a_large_import_a_normal_single_pin_save_is_unaffected(self) -> None:
        """The collector must not leak across calls: an ordinary save outside any import still enqueues immediately, per-item, on its own queue - not batched, not routed to bulk."""
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.reputation.model import ReputationEvent
        from urbanlens.dashboard.tasks import ensure_wiki_for_location, score_reputation_event

        self._run_import(5)

        location, _ = Location.objects.get_nearby_or_create(60.0, 60.0)
        with mock.patch(self.FOLLOW_ON_ENQUEUE) as enqueue:
            pin = Pin.objects.create(profile=self.profile, location=location, name="Solo add")

        event = ReputationEvent.objects.get(profile=self.profile, rule_key="pin_created", target_id=pin.pk)
        enqueue.assert_has_calls(
            [
                mock.call(ensure_wiki_for_location, location.pk, queue=None),
                mock.call(score_reputation_event, event.pk, queue=None),
            ],
            any_order=True,
        )
        self.assertEqual(
            enqueue.call_count,
            2,
            "a single non-batched pin save must enqueue its follow-on work immediately, one call per item - not chunked",
        )
