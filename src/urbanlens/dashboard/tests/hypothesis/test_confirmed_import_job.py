"""A confirmed import is a job: stored, queued once per account, polled, and cleaned up however it ends."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.import_export.import_data import import_dir
from urbanlens.dashboard.services.pins import confirmed_import
from urbanlens.dashboard.services.pins.confirmed_import import PAYLOAD_FILENAME, guard_key, run_confirmed_import
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
        with open(os.path.join(import_dir(job_id), PAYLOAD_FILENAME), encoding="utf-8") as handle:
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
        root = os.path.dirname(import_dir("probe"))
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
        self.assertFalse(os.path.exists(import_dir(job["job_id"])))
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_cancel_stops_the_import_at_its_next_progress_write(self) -> None:
        response, _ = self._queued(3)
        job = response.json()

        self.assertEqual(self.client.post(job["cancel_url"]).status_code, 202)
        with mock.patch.object(confirmed_import, "PROGRESS_EVERY", 1):
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
        self.assertFalse(os.path.exists(import_dir(job["job_id"])))
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))

    def test_a_task_that_finishes_before_the_request_does_still_frees_the_account(self) -> None:
        """Run inline, the task ends inside the enqueue - before the view returns."""
        with tasks_run_inline(run_confirmed_pin_import):
            response = self._post(_lists(2))

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self._pins(), 2)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["status"], "done")
        self.assertIsNone(single_flight.holder(guard_key(self.profile.pk)))
