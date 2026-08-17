"""A partial bulk suggestion action must report itself as partial.

The view skips ids it cannot act on (not the caller's, already handled, gone) and
logs any that raise, then returns ``processed`` alongside ``requested``. The page
reported only ``processed`` as a success, so accepting 3 of 5 looked exactly like
accepting 5 - the user is told a number with nothing to compare it to.

These tests pin the response contract the toast now depends on. Without both
numbers there is no way for the frontend to tell a whole batch from a partial one.
"""

from __future__ import annotations

from decimal import Decimal
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus


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

    # Accepting is the only action here that creates a Pin. Bulk accepts pass
    # fetch_if_missing=False down the chain so no live Google lookup happens
    # inside the request (see BulkAcceptDeferredNameResolutionTests, which
    # asserts that) - the _resolve_name patch stays as a network-guard backstop
    # so a regression fails on the assertion rather than by reaching the real
    # internet. safely_enqueue_task is stubbed (both the controller's
    # module-level reference and the source module) because eager mode would
    # run any dispatched backfill task inline.
    def test_accepting_marks_the_suggestions_handled(self) -> None:
        suggestion = self._suggestion()

        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
            mock.patch("urbanlens.dashboard.controllers.pin_suggestions.safely_enqueue_task"),
        ):
            payload = self._post("accept", [suggestion.pk]).json()

        self.assertEqual(payload["processed"], 1)
        suggestion.refresh_from_db()
        self.assertNotEqual(suggestion.status, PinSuggestionStatus.PENDING)
        self.assertTrue(Pin.objects.filter(profile=self.profile).exists())


class BulkAcceptDeferredNameResolutionTests(TestCase):
    """Bulk accepts must never make a live Google name lookup inside the request.

    Accepting a new-pin suggestion at coordinates with no existing Location
    used to resolve the place's canonical name synchronously - up to
    ``_MAX_BULK_SUGGESTIONS`` (200) sequential outbound calls in one
    request/response cycle. Both bulk endpoints now pass
    ``fetch_if_missing=False`` down the accept chain (the Location is created
    with ``official_name=None``) and instead dispatch
    ``tasks.resolve_location_place_name`` per name-less Location - the same
    lazy backfill PinOverviewView uses. Single accepts stay synchronous.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        # Every test here asserts on the same two things: no live Google name
        # lookup, and what the view dispatched. The controller imports
        # safely_enqueue_task at module level, so its own dispatches must be
        # patched there; the services.core.celery patch guards everything else
        # (signals import it at call time) from running inline under eager
        # mode - see tests/CLAUDE.md.
        self.mock_resolve_name = self.enterContext(
            mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None)
        )
        self.mock_view_enqueue = self.enterContext(mock.patch("urbanlens.dashboard.controllers.pin_suggestions.safely_enqueue_task"))
        self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))

    def _suggestion_at(self, lat: str, lon: str) -> PinSuggestion:
        """A pending new-pin suggestion at explicit coordinates (distinct ones create distinct Locations)."""
        return baker.make(
            PinSuggestion,
            profile=self.profile,
            status=PinSuggestionStatus.PENDING,
            origin=PinSuggestionOrigin.IMMICH,
            latitude=Decimal(lat),
            longitude=Decimal(lon),
        )

    def _bulk_accept(self, ids: list[int]):
        return self.client.post(
            reverse("memories.locations.bulk", args=["accept"]),
            data=json.dumps({"suggestion_ids": ids}),
            content_type="application/json",
        )

    def _resolution_dispatches(self) -> list[int]:
        """Location pks the view dispatched resolve_location_place_name for."""
        from urbanlens.dashboard.tasks import resolve_location_place_name

        return [call.args[1] for call in self.mock_view_enqueue.call_args_list if call.args and call.args[0] is resolve_location_place_name]

    def test_bulk_accept_never_calls_google_and_defers_name_resolution(self) -> None:
        suggestions = [self._suggestion_at("10.0", "10.0"), self._suggestion_at("11.0", "11.0"), self._suggestion_at("12.0", "12.0")]

        payload = self._bulk_accept([suggestion.pk for suggestion in suggestions]).json()

        self.assertEqual(payload["processed"], 3)
        self.mock_resolve_name.assert_not_called()
        pins = Pin.objects.filter(profile=self.profile)
        self.assertEqual(pins.count(), 3)
        self.assertCountEqual(self._resolution_dispatches(), [pin.location_id for pin in pins])

    def test_accept_all_never_calls_google_and_defers_name_resolution(self) -> None:
        suggestions = [self._suggestion_at("10.0", "10.0"), self._suggestion_at("11.0", "11.0")]

        payload = self.client.post(reverse("memories.locations.accept_all")).json()

        self.assertEqual(payload["processed"], len(suggestions))
        self.mock_resolve_name.assert_not_called()
        pins = Pin.objects.filter(profile=self.profile)
        self.assertEqual(pins.count(), len(suggestions))
        self.assertCountEqual(self._resolution_dispatches(), [pin.location_id for pin in pins])

    def test_no_resolution_is_dispatched_when_external_apis_are_disabled(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled", "updated"])
        suggestion = self._suggestion_at("10.0", "10.0")

        payload = self._bulk_accept([suggestion.pk]).json()

        self.assertEqual(payload["processed"], 1, "the accept itself must still go through")
        self.mock_resolve_name.assert_not_called()
        self.assertEqual(self._resolution_dispatches(), [])

    def test_an_already_named_location_gets_no_redundant_dispatch(self) -> None:
        from urbanlens.dashboard.models.google_place.model import GooglePlace
        from urbanlens.dashboard.models.location.model import Location

        google_place = baker.make(GooglePlace, latitude=Decimal("10.0"), longitude=Decimal("10.0"), cached_place_name="Real Place")
        baker.make(Location, latitude=Decimal("10.0"), longitude=Decimal("10.0"), google_place=google_place, official_name="Real Place")
        suggestion = self._suggestion_at("10.0", "10.0")

        payload = self._bulk_accept([suggestion.pk]).json()

        self.assertEqual(payload["processed"], 1)
        self.mock_resolve_name.assert_not_called()
        self.assertEqual(self._resolution_dispatches(), [])
