"""A partial bulk suggestion action must report itself as partial.

The view skips ids it cannot act on (not the caller's, already handled, gone) and
logs any that raise, then returns ``processed`` alongside ``requested``. The page
reported only ``processed`` as a success, so accepting 3 of 5 looked exactly like
accepting 5 - the user is told a number with nothing to compare it to.

These tests pin the response contract the toast now depends on. Without both
numbers there is no way for the frontend to tell a whole batch from a partial one.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionStatus


class BulkSuggestionPartialReportingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _suggestion(self) -> PinSuggestion:
        return baker.make(PinSuggestion, profile=self.profile, status=PinSuggestionStatus.PENDING)

    def _post(self, action: str, ids: list[int]):
        return self.client.post(
            reverse("memories.locations.bulk", args=[action]),
            data=json.dumps({"suggestion_ids": ids}),
            content_type="application/json",
        )

    def test_a_whole_batch_reports_processed_equal_to_requested(self) -> None:
        ids = [self._suggestion().pk for _ in range(3)]

        payload = self._post("reject", ids).json()

        self.assertEqual(payload["processed"], 3)
        self.assertEqual(payload["requested"], 3)

    def test_an_id_that_is_not_the_callers_is_counted_as_requested_but_not_processed(self) -> None:
        mine = self._suggestion()
        someone_elses = baker.make(PinSuggestion, profile=baker.make(User).profile, status=PinSuggestionStatus.PENDING)

        payload = self._post("reject", [mine.pk, someone_elses.pk]).json()

        self.assertEqual(payload["requested"], 2)
        self.assertEqual(payload["processed"], 1, "another profile's suggestion must not be acted on")
        someone_elses.refresh_from_db()
        self.assertEqual(someone_elses.status, PinSuggestionStatus.PENDING)

    def test_one_failing_item_does_not_stop_the_others_and_is_visible_in_the_counts(self) -> None:
        first, second = self._suggestion(), self._suggestion()
        failing_pk = first.pk

        def reject(suggestion: PinSuggestion) -> None:
            if suggestion.pk == failing_pk:
                raise RuntimeError("corrupt suggestion row")
            suggestion.status = PinSuggestionStatus.REJECTED
            suggestion.save(update_fields=["status", "updated"])

        with mock.patch("urbanlens.dashboard.controllers.pin_suggestions.reject_pin_suggestion", side_effect=reject):
            payload = self._post("reject", [first.pk, second.pk]).json()

        self.assertEqual(payload["requested"], 2)
        self.assertEqual(payload["processed"], 1, "the healthy suggestion must still be processed")
        second.refresh_from_db()
        self.assertEqual(second.status, PinSuggestionStatus.REJECTED)

    # Accepting is the only action here that creates a Pin, and creating one at
    # coordinates with no existing Location goes through
    # _create_location_with_canonical_name -> GooglePlaceService._resolve_name,
    # which is a live outbound lookup made *synchronously inside the request*.
    # Unmocked it reaches the real internet and the suite's network guard fails
    # the test. Same patch pair as test_photo_organize, which hits this path for
    # the same reason.
    @mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
    @mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
    def test_accepting_marks_the_suggestions_handled(self, _mock_enqueue, _mock_resolve_name) -> None:
        suggestion = self._suggestion()

        payload = self._post("accept", [suggestion.pk]).json()

        self.assertEqual(payload["processed"], 1)
        suggestion.refresh_from_db()
        self.assertNotEqual(suggestion.status, PinSuggestionStatus.PENDING)
        self.assertTrue(Pin.objects.filter(profile=self.profile).exists())
