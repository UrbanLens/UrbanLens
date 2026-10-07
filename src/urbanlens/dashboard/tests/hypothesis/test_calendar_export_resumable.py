"""A trip's Google Calendar export resumes where the budget stopped it, and never sends an event twice (P334).

Measured before the fix with the real limiter and the HTTP layer stubbed: a trip with 40 scheduled activities under
the site-wide 30-a-minute budget sent 30 requests per attempt (the trip event and the first 29 activities) and was
refused at the same place every time, so the last 11 activities never reached the calendar. These tests run the same
shape - the real gateway, session and limiter, with only ``requests.Session.request`` replaced by an in-memory
calendar - and assert that the continuation writes the rest instead.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any
from unittest import mock
import uuid

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
import requests

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import client_event_id
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.trips import calendar_sync
from urbanlens.dashboard.services.trips.calendar_sync import (
    export_trip_to_calendar,
    push_auto_synced_trip_changes,
    remove_trip_from_calendar,
    trip_event_id,
)
from urbanlens.dashboard.tasks import MAX_CALENDAR_PUSH_ATTEMPTS, push_trip_to_calendar, requeue_pending_calendar_pushes

SERVICE = "google_calendar"
_EVENT_PATH = re.compile(r"/calendars/[^/]+/events(?:/(?P<event_id>[^/?]+))?$")
_BASE32HEX = re.compile(r"^[a-v0-9]{5,1024}$")


def _response(status: int, payload: dict[str, Any] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode() if payload is not None else b""
    response.headers["Content-Type"] = "application/json"
    return response


class FakeGoogleCalendar:
    """One calendar's events, answered the way the Calendar API answers.

    A deleted event keeps its id, as Google's do: it is marked cancelled, a create reusing the id is refused with 409,
    and a patch can bring it back.

    Attributes:
        events: Event resources by id, cancelled ones included.
        requests: ``(method, event id or None, json body)`` for every request that reached the calendar.
        lose_next_create_response: Store the next created event, then raise as if its response were lost.
        fail_after: Answer 500 to every request after this many.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str | None, dict[str, Any] | None]] = []
        self.lose_next_create_response = False
        self.fail_after: int | None = None

    def __call__(self, _session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
        match = _EVENT_PATH.search(url.split("?", 1)[0])
        if match is None:
            raise AssertionError(f"unexpected Google Calendar request {method} {url}")
        event_id = match.group("event_id")
        body = kwargs.get("json")
        self.requests.append((method, event_id, body))
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return _response(500, {"error": {"message": "backend error"}})
        if method == "POST":
            return self._insert(dict(body or {}))
        if method == "PATCH":
            event = self.events.get(event_id or "")
            if event is None:
                return _response(404, {"error": {"message": "Not Found"}})
            event.update(body or {})
            return _response(200, event)
        if method == "DELETE":
            event = self.events.get(event_id or "")
            if event is None or event.get("status") == "cancelled":
                return _response(410, {"error": {"message": "Resource has been deleted"}})
            event["status"] = "cancelled"
            return _response(204)
        raise AssertionError(f"unexpected method {method}")

    def _insert(self, body: dict[str, Any]) -> requests.Response:
        event_id = body.get("id") or uuid.uuid4().hex
        if event_id in self.events:
            return _response(409, {"error": {"message": "The requested identifier already exists."}})
        event = {"status": "confirmed", **body, "id": event_id}
        self.events[event_id] = event
        if self.lose_next_create_response:
            self.lose_next_create_response = False
            raise requests.ConnectionError("connection reset after the request was sent")
        return _response(200, event)

    def live_events(self) -> list[dict[str, Any]]:
        """Events on the calendar that are not cancelled."""
        return [event for event in self.events.values() if event.get("status") != "cancelled"]

    def creates_by_activity(self) -> dict[str | None, int]:
        """How many times a create was sent for each activity id (None for the trip's all-day event)."""
        counts: dict[str | None, int] = {}
        for method, _event_id, body in self.requests:
            if method == "POST" and body is not None:
                activity_id = body["extendedProperties"]["private"].get("urbanlens_activity_id")
                counts[activity_id] = counts.get(activity_id, 0) + 1
        return counts


class _CalendarExportCase(TestCase):
    """A profile with a connected calendar, a dated trip, and the HTTP layer swapped for a fake calendar."""

    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="p334-exporter")
        self.profile = Profile.objects.get(user=self.user)
        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106 - fixture value
            refresh_token="refresh",  # noqa: S106 - fixture value
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.google = FakeGoogleCalendar()
        patcher = mock.patch.object(requests.Session, "request", autospec=True, side_effect=self.google)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Continuations are queued by the sweep; capture them rather than run Celery.
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

    def _limit(self, per_minute: int) -> None:
        config = rate_limiter.get_limit_config(SERVICE)
        config.calls_per_minute = per_minute
        config.save(update_fields=["calls_per_minute", "updated"])

    def _trip(self, activities: int) -> Trip:
        trip = Trip.objects.create(
            name="Long weekend",
            creator=self.profile,
            start_date=datetime.date(2026, 11, 6),
            end_date=datetime.date(2026, 11, 8),
        )
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED, rsvp="yes")
        first = datetime.datetime(2026, 11, 6, 8, 0, tzinfo=datetime.UTC)
        TripActivity.objects.bulk_create(
            [
                TripActivity(
                    trip=trip,
                    added_by=self.profile,
                    title=f"Stop {index}",
                    order=index,
                    scheduled_at=first + datetime.timedelta(hours=index),
                )
                for index in range(activities)
            ]
        )
        return trip

    @staticmethod
    def _let_the_minute_pass() -> None:
        """Age every recorded call out of the per-minute window, as the measurement did between attempts."""
        ApiCallLog.objects.filter(service=SERVICE).update(created=timezone.now() - datetime.timedelta(minutes=2))

    def _age_push_request(self, trip: Trip) -> None:
        TripCalendarLink.objects.filter(trip=trip, activity__isnull=True, push_requested_at__isnull=False).update(
            push_requested_at=timezone.now() - datetime.timedelta(hours=1)
        )

    def _trip_link(self, trip: Trip) -> TripCalendarLink:
        return TripCalendarLink.objects.get(trip=trip, profile=self.profile, activity__isnull=True)


class FortyActivitiesUnderThirtyAMinuteTests(_CalendarExportCase):
    """P334's measurement, now finishing: the first attempt writes 30, the sweep's push writes the other 11."""

    def test_the_continuation_writes_the_rest_and_no_activity_is_sent_twice(self) -> None:
        self._limit(30)
        trip = self._trip(40)

        first = export_trip_to_calendar(self.account, trip)

        self.assertFalse(first.complete)
        self.assertEqual((first.written, first.events_synced, first.events_total), (30, 30, 41))
        self.assertEqual(first.activities_synced, 29)
        self.assertEqual(len(self.google.live_events()), 30)
        self.assertIsNotNone(self._trip_link(trip).push_requested_at)

        self._let_the_minute_pass()
        self._age_push_request(trip)
        self.assertEqual(requeue_pending_calendar_pushes(), 1)
        self.enqueue.assert_called_once_with(push_trip_to_calendar, trip.pk, durable=False)
        self.assertEqual(push_trip_to_calendar(trip.pk), 1)

        self.assertEqual(len(self.google.live_events()), 41)
        self.assertEqual(len(self.google.requests), 41)
        creates = self.google.creates_by_activity()
        self.assertEqual(len(creates), 41)
        self.assertEqual(set(creates.values()), {1})
        self.assertEqual(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).count(), 41)
        link = self._trip_link(trip)
        self.assertIsNone(link.push_requested_at)
        self.assertFalse(link.auto_sync)

    def test_an_attempt_resumes_without_the_sweep_too(self) -> None:
        """Pressing export again (or the API) continues from the cut, rather than re-sending the first 30."""
        self._limit(30)
        trip = self._trip(40)
        export_trip_to_calendar(self.account, trip)
        self._let_the_minute_pass()

        second = export_trip_to_calendar(self.account, trip)

        self.assertTrue(second.complete)
        self.assertEqual((second.written, second.events_synced), (11, 41))
        self.assertEqual(len(self.google.requests), 41)


class OneFullTripFitsTheRaisedLimitTests(_CalendarExportCase):
    def test_the_default_minute_holds_a_trip_at_max_trip_activities(self) -> None:
        default_max = SiteSettings._meta.get_field("max_trip_activities").default
        self.assertGreaterEqual(rate_limiter.get_limit_config(SERVICE).calls_per_minute, default_max + 1)

    def test_a_100_activity_trip_finishes_in_one_attempt(self) -> None:
        trip = self._trip(100)

        result = export_trip_to_calendar(self.account, trip)

        self.assertTrue(result.complete)
        self.assertEqual((result.written, result.events_synced, result.events_total), (101, 101, 101))
        self.assertEqual(len(self.google.live_events()), 101)
        self.assertIsNone(self._trip_link(trip).push_requested_at)


class ClientAssignedEventIdTests(_CalendarExportCase):
    def test_a_lost_create_response_is_retried_into_a_409_and_linked_without_a_duplicate(self) -> None:
        trip = self._trip(1)
        activity = trip.activities.get()
        expected_id = trip_event_id(trip, self.profile, activity)
        export_trip_to_calendar(self.account, trip)
        # The trip event is linked; take the activity's link away and lose the answer to its re-create.
        TripCalendarLink.objects.filter(activity=activity).delete()
        del self.google.events[expected_id]
        self.google.lose_next_create_response = True

        with self.assertRaises(GatewayRequestError):
            export_trip_to_calendar(self.account, trip)
        self.assertIn(expected_id, self.google.events)
        self.assertFalse(TripCalendarLink.objects.filter(activity=activity).exists())

        result = export_trip_to_calendar(self.account, trip)

        self.assertTrue(result.complete)
        self.assertEqual(len(self.google.live_events()), 2)
        self.assertEqual(TripCalendarLink.objects.get(activity=activity).google_event_id, expected_id)
        method, event_id, body = self.google.requests[-1]
        self.assertEqual((method, event_id, body["status"]), ("PATCH", expected_id, "confirmed"))
        sent_ids = [body["id"] for method, _id, body in self.google.requests if method == "POST" and body.get("id")]
        self.assertEqual(sent_ids.count(expected_id), 3)

    def test_re_adding_a_removed_trip_restores_its_deleted_events(self) -> None:
        """Google keeps a deleted event's id, so the re-create meets 409 and brings the event back."""
        trip = self._trip(2)
        export_trip_to_calendar(self.account, trip)
        remove_trip_from_calendar(self.account, trip)
        self.assertEqual(self.google.live_events(), [])

        result = export_trip_to_calendar(self.account, trip)

        self.assertTrue(result.complete)
        self.assertEqual(len(self.google.events), 3)
        self.assertEqual(len(self.google.live_events()), 3)

    def test_ids_are_valid_google_ids_and_distinct_per_profile_and_activity(self) -> None:
        trip = self._trip(2)
        first, second = trip.activities.all()
        other = User.objects.create_user(username="p334-mate").profile
        ids = {
            trip_event_id(trip, self.profile, None),
            trip_event_id(trip, self.profile, first),
            trip_event_id(trip, self.profile, second),
            trip_event_id(trip, other, first),
        }
        self.assertEqual(len(ids), 4)
        for event_id in ids:
            self.assertRegex(event_id, _BASE32HEX)
        self.assertEqual(trip_event_id(trip, self.profile, first), trip_event_id(trip, self.profile, first))


class ClientEventIdTests(SimpleTestCase):
    @given(parts=st.lists(st.text(max_size=40), min_size=1, max_size=4))
    def test_any_parts_give_a_valid_reproducible_id(self, parts: list[str]) -> None:
        event_id = client_event_id(*parts)
        self.assertRegex(event_id, _BASE32HEX)
        self.assertEqual(event_id, client_event_id(*parts))

    def test_parts_are_not_merely_concatenated(self) -> None:
        self.assertNotEqual(client_event_id("a", "bc"), client_event_id("ab", "c"))


class AttemptCapCountsOnlyPushesThatWentNowhereTests(_CalendarExportCase):
    def setUp(self) -> None:
        super().setUp()
        self._limit(30)
        self.trip = self._trip(40)
        self.link = TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
            push_requested_at=timezone.now() - datetime.timedelta(hours=1),
            push_attempts=MAX_CALENDAR_PUSH_ATTEMPTS - 1,
        )

    def _attempts(self) -> int:
        self.link.refresh_from_db()
        return self.link.push_attempts

    def test_a_push_that_wrote_events_resets_the_count_and_one_that_wrote_none_adds_to_it(self) -> None:
        self.assertEqual(push_auto_synced_trip_changes(self.trip), 0)
        self.assertEqual(len(self.google.requests), 30)
        self.assertEqual(self._attempts(), 0)

        # The minute is still spent: the next push writes nothing.
        self.assertEqual(push_auto_synced_trip_changes(self.trip), 0)
        self.assertEqual(len(self.google.requests), 30)
        self.assertEqual(self._attempts(), 1)
        self.assertIsNotNone(self.link.push_requested_at)

    def test_pushes_that_go_nowhere_reach_the_cap_and_the_sweep_drops_the_request(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=SERVICE, success=True) for _ in range(30)])

        push_auto_synced_trip_changes(self.trip)

        self.assertEqual(self.google.requests, [])
        self.assertEqual(self._attempts(), MAX_CALENDAR_PUSH_ATTEMPTS)
        self.assertEqual(requeue_pending_calendar_pushes(), 0)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)

    def test_a_failure_after_some_writes_does_not_count_and_one_before_any_does(self) -> None:
        self.google.fail_after = 5

        push_auto_synced_trip_changes(self.trip)
        self.assertEqual(self._attempts(), 0)

        self._let_the_minute_pass()
        self.google.fail_after = len(self.google.requests)
        push_auto_synced_trip_changes(self.trip)
        self.assertEqual(self._attempts(), 1)

    def test_a_finished_push_settles_the_request(self) -> None:
        self._limit(120)

        self.assertEqual(push_auto_synced_trip_changes(self.trip), 1)

        self.link.refresh_from_db()
        self.assertEqual((self.link.push_requested_at, self.link.push_attempts), (None, 0))


class PartialProgressIsReportedTests(_CalendarExportCase):
    def setUp(self) -> None:
        super().setUp()
        self._limit(30)
        self.trip = self._trip(40)

    def _toast(self, response) -> dict[str, str]:
        return json.loads(response["HX-Trigger"])["showToast"]

    def test_the_export_button_says_how_many_are_on_the_calendar_and_that_the_rest_will_follow(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(response.status_code, 200)
        toast = self._toast(response)
        self.assertEqual(toast["level"], "info")
        self.assertIn("30 of 41", toast["message"])
        self.assertIn("rest will follow", toast["message"])
        self.assertIsNotNone(self._trip_link(self.trip).push_requested_at)

    def test_the_export_button_says_busy_when_nothing_could_be_written(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=SERVICE, success=True) for _ in range(30)])
        self.client.force_login(self.user)

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(self._toast(response)["message"], calendar_sync.CALENDAR_BUSY_MESSAGE)
        self.assertFalse(TripCalendarLink.objects.filter(trip=self.trip).exists())

    def _api_post(self):
        api_key, raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]
        )
        return self.client.post(
            reverse("external_api:trips.calendar_export", args=[self.trip.slug]),
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )

    def test_the_api_reports_an_incomplete_export_with_its_counts(self) -> None:
        response = self._api_post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            {key: body[key] for key in ("complete", "events_synced", "events_total", "activities_exported")},
            {"complete": False, "events_synced": 30, "events_total": 41, "activities_exported": 29},
        )
        self.assertTrue(body["calendar"]["linked"])

    def test_the_api_answers_503_with_retry_after_when_nothing_could_be_written(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=SERVICE, success=True) for _ in range(30)])

        response = self._api_post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "60")
        self.assertEqual(response.json()["error"], calendar_sync.CALENDAR_BUSY_MESSAGE)

    def test_a_refusal_before_the_trip_has_a_link_is_raised_not_reported(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=SERVICE, success=True) for _ in range(30)])

        with self.assertRaises(RateLimitExceededError):
            export_trip_to_calendar(self.account, self.trip)
