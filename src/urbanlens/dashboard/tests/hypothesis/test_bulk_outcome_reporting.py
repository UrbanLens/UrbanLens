"""A per-row bulk endpoint reports rows that failed apart from rows it skipped (N29 G1-15).

The suggestion and unlogged-visit bulk endpoints answered ``ok: true`` whatever happened, and ``requested -
processed`` lumped a crashed row in with an id that was simply already handled, so the page blamed every shortfall on
"may have already been handled". A row that crashed halfway could also leave half its writes behind.
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit


class BulkOutcomeTests(SimpleTestCase):
    def test_skipped_is_what_was_neither_processed_nor_failed(self) -> None:
        from urbanlens.dashboard.services.core.bulk_outcome import BulkOutcome

        outcome = BulkOutcome(requested=5, processed=2, failed=1)
        self.assertEqual(outcome.skipped, 2)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.as_json(), {"ok": False, "requested": 5, "processed": 2, "failed": 1, "skipped": 2})

    def test_run_each_counts_and_keeps_going(self) -> None:
        from urbanlens.dashboard.services.core.bulk_outcome import run_each

        def action(item: int) -> None:
            if item == 2:
                raise RuntimeError("boom")

        with mock.patch("urbanlens.dashboard.services.core.bulk_outcome.transaction.atomic"):
            outcome = run_each([1, 2, 3], action, requested=4, description="test")
        self.assertEqual((outcome.processed, outcome.failed, outcome.skipped), (2, 1, 1))


class _SuggestionCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _suggestion(self) -> PinSuggestion:
        return baker.make(PinSuggestion, profile=self.profile, status=PinSuggestionStatus.PENDING)


class SuggestionBulkTests(_SuggestionCase):
    def _post(self, action: str, ids: list[int]):
        return self.client.post(
            reverse("memories.locations.bulk", args=[action]),
            data=json.dumps({"suggestion_ids": ids}),
            content_type="application/json",
        )

    def test_a_crashed_row_is_failed_not_skipped(self) -> None:
        first, second = self._suggestion(), self._suggestion()

        def reject(suggestion: PinSuggestion) -> None:
            if suggestion.pk == first.pk:
                raise RuntimeError("corrupt row")
            suggestion.status = PinSuggestionStatus.REJECTED
            suggestion.save(update_fields=["status", "updated"])

        with mock.patch("urbanlens.dashboard.controllers.pin_suggestions.reject_pin_suggestion", side_effect=reject):
            payload = self._post("reject", [first.pk, second.pk]).json()

        self.assertEqual((payload["processed"], payload["failed"], payload["skipped"]), (1, 1, 0))
        self.assertFalse(payload["ok"])

    def test_an_id_that_is_not_actionable_is_skipped_not_failed(self) -> None:
        mine = self._suggestion()
        theirs = baker.make(PinSuggestion, profile=baker.make(User).profile, status=PinSuggestionStatus.PENDING)

        payload = self._post("reject", [mine.pk, theirs.pk]).json()

        self.assertEqual((payload["processed"], payload["failed"], payload["skipped"]), (1, 0, 1))
        self.assertTrue(payload["ok"])


class AcceptAllTests(_SuggestionCase):
    def test_a_crashed_row_is_reported(self) -> None:
        first, _second = self._suggestion(), self._suggestion()

        def accept(suggestion: PinSuggestion, profile, *, resolve_names_async: bool) -> None:
            if suggestion.pk == first.pk:
                raise RuntimeError("corrupt row")

        with (
            mock.patch(
                "urbanlens.dashboard.controllers.pin_suggestions._pending_suggestions",
                return_value=PinSuggestion.objects.filter(profile=self.profile),
            ),
            mock.patch("urbanlens.dashboard.controllers.pin_suggestions._bulk_accept_suggestion", side_effect=accept),
        ):
            payload = self.client.post(reverse("memories.locations.accept_all")).json()

        self.assertEqual((payload["processed"], payload["failed"]), (1, 1))
        self.assertFalse(payload["ok"])


class UnloggedVisitBulkTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_a_row_that_crashes_halfway_keeps_none_of_its_writes(self) -> None:
        from urbanlens.dashboard.services.visits import visits

        pins = baker.make(
            Pin, profile=self.profile, last_visited=datetime.datetime(2024, 6, 1, tzinfo=datetime.UTC), _quantity=2
        )
        failing = pins[0]
        real_add = visits.add_visited_status

        def add_visited_status(pin: Pin) -> None:
            if pin.pk == failing.pk:
                raise RuntimeError("label write failed")
            real_add(pin)

        with (
            mock.patch("urbanlens.dashboard.controllers.memories.add_visited_status", side_effect=add_visited_status),
            mock.patch("urbanlens.dashboard.controllers.memories.visit_logging_allowed", return_value=True),
            mock.patch("urbanlens.dashboard.models.pin.queryset.PinQuerySet.visited_without_record", lambda qs: qs),
        ):
            payload = self.client.post(
                reverse("memories.visits.bulk", args=["log"]),
                data=json.dumps({"pin_slugs": [pin.slug for pin in pins]}),
                content_type="application/json",
            ).json()

        self.assertEqual((payload["processed"], payload["failed"]), (1, 1))
        self.assertFalse(PinVisit.objects.filter(pin=failing).exists(), "the crashed row's visit was committed")
        self.assertTrue(PinVisit.objects.filter(pin=pins[1]).exists())
