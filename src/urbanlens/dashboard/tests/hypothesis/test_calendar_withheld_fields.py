"""What a calendar export withholds is removed from the Google event (P335), and a Google rate limit is not a dead grant.

An update is a PATCH, and Google keeps every field a PATCH leaves out. A location the export stopped sending - hidden
after export, or restricted by the trip-mate who added it - stayed on the event. These tests run the export against
``FakeGoogleCalendar``, whose PATCH merges the body into the stored event the way Google's does.

Google answers a rate or usage limit with 403 as well as 429 ("Handle API errors", Calendar API guides). The gateway
read every 403 as a revoked grant, and the callers deleted the connection.
"""

from __future__ import annotations

import datetime
import json
import string
from typing import Any

from django.contrib.auth.models import User
from django.urls import reverse
import requests

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import (
    CalendarRateLimitedError,
    GoogleCalendarGateway,
    is_rate_limit_refusal,
)
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
from urbanlens.dashboard.services.trips import calendar_sync
from urbanlens.dashboard.services.trips.calendar_sync import (
    activity_to_event_body,
    export_trip_to_calendar,
    push_auto_synced_trip_changes,
)
from urbanlens.dashboard.services.trips.trip_activities import create_activity
from urbanlens.dashboard.services.trips.trip_visibility import HIDDEN_ACTIVITY_TITLE
from urbanlens.dashboard.tasks import push_trip_to_calendar
from urbanlens.dashboard.tests.hypothesis.test_calendar_export_resumable import _CalendarExportCase

_ADDRESS_COMPONENTS = {
    "street_number": "1580",
    "route": "E Grand Blvd",
    "locality": "Detroit",
    "administrative_area_level_1": "MI",
}
_ADDRESS = "1580 E Grand Blvd, Detroit, MI"
_PLACE_NAME = "Packard Plant"
_IMPORTED_LOCATION = "123 Factory Rd, Utica, NY"


def _google_error(status: int, reason: str, message: str, *, domain: str = "usageLimits") -> dict[str, Any]:
    """An error body in the shape the Calendar API guide documents for *reason*."""
    return {
        "error": {
            "errors": [{"domain": domain, "reason": reason, "message": message}],
            "code": status,
            "message": message,
        }
    }


RATE_LIMIT = _google_error(403, "rateLimitExceeded", "Rate Limit Exceeded")
USER_RATE_LIMIT = _google_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded")
USAGE_LIMIT = _google_error(403, "quotaExceeded", "Calendar usage limits exceeded.")
TOO_MANY_REQUESTS = _google_error(429, "rateLimitExceeded", "Rate Limit Exceeded")
#: The per-user quota refusal in Google's newer envelope: ``status`` is PERMISSION_DENIED even though it is a rate limit.
PER_USER_QUOTA = {
    "error": {
        "code": 403,
        "message": (
            "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user' of service "
            "'calendar-json.googleapis.com' for consumer 'project_number:123456789012'."
        ),
        "errors": [{"message": "Rate Limit Exceeded", "domain": "usageLimits", "reason": "rateLimitExceeded"}],
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "domain": "googleapis.com",
                "metadata": {
                    "service": "calendar-json.googleapis.com",
                    "quota_metric": "calendar-json.googleapis.com/default",
                    "quota_limit": "defaultPerMinutePerUser",
                    "quota_limit_value": "600",
                    "consumer": "projects/123456789012",
                },
            }
        ],
    }
}
INSUFFICIENT_SCOPE = {
    "error": {
        "code": 403,
        "message": "Request had insufficient authentication scopes.",
        "errors": [{"message": "Insufficient Permission", "domain": "global", "reason": "insufficientPermissions"}],
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                "domain": "googleapis.com",
                "metadata": {"service": "calendar-json.googleapis.com", "method": "calendar.v3.Events.Insert"},
            }
        ],
    }
}
FORBIDDEN = _google_error(403, "forbidden", "Forbidden", domain="global")
INVALID_CREDENTIALS = {
    "error": {
        "errors": [
            {
                "domain": "global",
                "reason": "authError",
                "message": "Invalid Credentials",
                "locationType": "header",
                "location": "Authorization",
            }
        ],
        "code": 401,
        "message": "Invalid Credentials",
    }
}

_RATE_LIMITS = {
    "rateLimitExceeded": (403, RATE_LIMIT),
    "userRateLimitExceeded": (403, USER_RATE_LIMIT),
    "quotaExceeded": (403, USAGE_LIMIT),
    "per-user quota, newer envelope": (403, PER_USER_QUOTA),
    "429": (429, TOO_MANY_REQUESTS),
}
_DEAD_GRANTS = {
    "401 authError": (401, INVALID_CREDENTIALS),
    "insufficientPermissions": (403, INSUFFICIENT_SCOPE),
    "forbidden": (403, FORBIDDEN),
}


def _raw_response(status: int, content: bytes, content_type: str = "application/json") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = content
    response.headers["Content-Type"] = content_type
    return response


class _TripWithAMateCase(_CalendarExportCase):
    """A dated trip the exporter shares with a trip-mate, exported to the exporter's fake calendar."""

    def setUp(self) -> None:
        super().setUp()
        self.trip = self._trip(0)
        self.mate = User.objects.create_user(username="p335-mate").profile
        TripMembership.objects.create(
            trip=self.trip, profile=self.mate, status=TripMembership.STATUS_JOINED, rsvp="yes"
        )

    def _located_activity(self, *, added_by: Profile) -> TripActivity:
        """A scheduled stop at a named, addressed place, titled by nothing but that place."""
        location = Location.objects.create(
            latitude=42.380, longitude=-83.035, official_name=_PLACE_NAME, **_ADDRESS_COMPONENTS
        )
        return TripActivity.objects.create(
            trip=self.trip,
            added_by=added_by,
            location=location,
            scheduled_at=datetime.datetime(2026, 11, 6, 9, 0, tzinfo=datetime.UTC),
        )

    def _set_mate_visibility(self, visibility: str) -> None:
        Profile.objects.filter(pk=self.mate.pk).update(trip_pin_location_visibility=visibility)

    def _link(self, activity: TripActivity | None) -> TripCalendarLink:
        return TripCalendarLink.objects.get(trip=self.trip, profile=self.profile, activity=activity)

    def _event(self, activity: TripActivity | None) -> dict[str, Any]:
        return self.google.events[self._link(activity).google_event_id]

    def _assert_nothing_names_the_place(self, event: dict[str, Any]) -> None:
        text = json.dumps(event)
        for leak in (_ADDRESS, "Grand Blvd", _PLACE_NAME, "42.38", "-83.03"):
            self.assertNotIn(leak, text)


class AWithheldLocationIsRemovedFromTheEventTests(_TripWithAMateCase):
    def test_hiding_an_exported_location_clears_it_and_masks_the_title(self) -> None:
        activity = self._located_activity(added_by=self.profile)
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(activity)["location"], _ADDRESS)
        self.assertEqual(self._event(None)["location"], _ADDRESS)
        self.assertEqual(self._event(activity)["summary"], f"Long weekend: {_PLACE_NAME}")
        fingerprints = {self._link(activity).event_fingerprint, self._link(None).event_fingerprint}
        sent_before = len(self.google.requests)

        activity.location_hidden = True
        activity.save(update_fields=["location_hidden", "updated"])
        result = export_trip_to_calendar(self.account, self.trip)

        self.assertTrue(result.complete)
        self.assertEqual(result.written, 2)
        patches = [body for method, _id, body in self.google.requests[sent_before:] if method == "PATCH"]
        self.assertEqual([body["location"] for body in patches], ["", ""])
        for event in (self._event(activity), self._event(None)):
            self.assertEqual(event["location"], "")
            self.assertNotIn(_PLACE_NAME, event["description"])
            self._assert_nothing_names_the_place(event)
        self.assertEqual(self._event(activity)["summary"], f"Long weekend: {HIDDEN_ACTIVITY_TITLE}")
        # The withheld state is part of what the fingerprint hashes: both links now vouch for the cleared bodies.
        self.assertTrue(
            fingerprints.isdisjoint({self._link(activity).event_fingerprint, self._link(None).event_fingerprint})
        )
        self.assertEqual(export_trip_to_calendar(self.account, self.trip).written, 0)

    def test_a_trip_mate_restricting_their_location_clears_it_from_the_exporters_events(self) -> None:
        self._set_mate_visibility(VisibilityChoice.ANYONE)
        activity = self._located_activity(added_by=self.mate)
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(activity)["location"], _ADDRESS)
        before = self._link(activity).event_fingerprint

        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        export_trip_to_calendar(self.account, self.trip)

        for event in (self._event(activity), self._event(None)):
            self.assertEqual(event["location"], "")
            self._assert_nothing_names_the_place(event)
        self.assertNotEqual(self._link(activity).event_fingerprint, before)

    def test_a_location_shown_again_is_written_back(self) -> None:
        activity = self._located_activity(added_by=self.profile)
        TripActivity.objects.filter(pk=activity.pk).update(location_hidden=True)
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(activity)["location"], "")

        TripActivity.objects.filter(pk=activity.pk).update(location_hidden=False)
        export_trip_to_calendar(self.account, self.trip)

        self.assertEqual(self._event(activity)["location"], _ADDRESS)
        self.assertEqual(self._event(activity)["summary"], f"Long weekend: {_PLACE_NAME}")

    def test_an_exported_event_whose_stop_lost_its_location_is_cleared_too(self) -> None:
        """UrbanLens created the event, so a location it no longer has is not someone else's to keep."""
        activity = self._located_activity(added_by=self.profile)
        TripActivity.objects.filter(pk=activity.pk).update(title="Meet up")
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(activity)["location"], _ADDRESS)

        TripActivity.objects.filter(pk=activity.pk).update(location=None)
        export_trip_to_calendar(self.account, self.trip)

        self.assertEqual(self._event(activity)["location"], "")
        self.assertEqual(self._event(None)["location"], "")

    def test_a_stop_picked_from_a_place_search_is_not_named_by_the_title_it_was_given(self) -> None:
        """An untitled stop added from a place search stores the place's name as its title (P186, P338)."""
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        activity = create_activity(
            self.trip,
            self.mate,
            place={"geocoded_lat": "42.380", "geocoded_lng": "-83.035", "geocoded_name": _PLACE_NAME},
            scheduled_at=datetime.datetime(2026, 11, 6, 9, 0, tzinfo=datetime.UTC),
        )
        self.assertEqual(activity.title, _PLACE_NAME)

        export_trip_to_calendar(self.account, self.trip)

        event = self._event(activity)
        self.assertEqual((event["summary"], event["location"]), (f"Long weekend: {HIDDEN_ACTIVITY_TITLE}", ""))
        self._assert_nothing_names_the_place(event)


class AnImportedEventKeepsALocationItCameWithTests(_TripWithAMateCase):
    """An imported trip's link points at the user's own event, whose location UrbanLens never had as a location."""

    def setUp(self) -> None:
        super().setUp()
        self.google.events["imported-mill"] = {
            "id": "imported-mill",
            "status": "confirmed",
            "summary": "Explore mill",
            "location": _IMPORTED_LOCATION,
            "start": {"date": "2026-11-06"},
            "end": {"date": "2026-11-09"},
        }
        TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="imported-mill",
            direction=CalendarSyncDirection.IMPORTED,
            auto_sync=True,
        )
        # What _create_activity_from_event makes of an all-day event's location: a title, not a place.
        TripActivity.objects.create(trip=self.trip, added_by=self.profile, title=_IMPORTED_LOCATION)

    def test_a_push_with_no_location_of_its_own_leaves_the_events_location_alone(self) -> None:
        Trip.objects.filter(pk=self.trip.pk).update(name="Mill weekend")
        self.trip.refresh_from_db()

        self.assertEqual(push_auto_synced_trip_changes(self.trip), 1)

        method, event_id, body = self.google.requests[-1]
        self.assertEqual((method, event_id), ("PATCH", "imported-mill"))
        self.assertNotIn("location", body)
        event = self.google.events["imported-mill"]
        self.assertEqual((event["summary"], event["location"]), ("Mill weekend", _IMPORTED_LOCATION))

    def test_a_location_the_push_withholds_is_still_cleared(self) -> None:
        self._set_mate_visibility(VisibilityChoice.ANYONE)
        activity = self._located_activity(added_by=self.mate)
        TripActivity.objects.filter(pk=activity.pk).update(scheduled_at=None)
        push_auto_synced_trip_changes(self.trip)
        self.assertEqual(self.google.events["imported-mill"]["location"], _ADDRESS)

        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        push_auto_synced_trip_changes(self.trip)

        self.assertEqual(self.google.events["imported-mill"]["location"], "")


class AHiddenActivityBodyNamesNothingOfThePlaceTests(SimpleTestCase):
    """Whatever the place is called and wherever it is, a hidden stop's event body carries none of it."""

    # "Zq" occurs nowhere else in a body, so a generated value found in it can only have come from the place.
    _token = st.text(alphabet=string.ascii_letters, min_size=3, max_size=12).map(lambda text: f"Zq{text}")

    @given(
        name=_token,
        route=_token,
        locality=_token,
        latitude=st.floats(min_value=-89, max_value=89, allow_nan=False).filter(lambda value: abs(value) > 0.01),
        longitude=st.floats(min_value=-179, max_value=179, allow_nan=False).filter(lambda value: abs(value) > 0.01),
        overridden=st.booleans(),
        title=st.one_of(st.none(), st.just(""), _token),
    )
    def test_a_hidden_stop_sends_an_empty_location_and_a_masked_title(
        self,
        name: str,
        route: str,
        locality: str,
        latitude: float,
        longitude: float,
        overridden: bool,
        title: str | None,
    ) -> None:
        location = Location(latitude=latitude, longitude=longitude, official_name=name, route=route, locality=locality)
        activity = TripActivity(
            trip=Trip(name="Mill weekend"),
            location=location,
            title=title,
            location_hidden=True,
            lat_override=latitude if overridden else None,
            lng_override=longitude if overridden else None,
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )

        body = activity_to_event_body(activity)

        assert body is not None
        self.assertEqual(body["location"], "")
        # A title may be the place's own name (a place search's), so a hidden stop's title is never sent.
        self.assertEqual(body["summary"], f"Mill weekend: {HIDDEN_ACTIVITY_TITLE}")
        text = json.dumps(body)
        self.assertNotIn("Zq", text)
        for coordinate in (latitude, longitude):
            self.assertNotIn(f"{coordinate:.6f}", text)

    def test_a_stop_with_no_place_sends_an_empty_location_only_for_an_event_urbanlens_made(self) -> None:
        activity = TripActivity(
            trip=Trip(name="Mill weekend"),
            title="Meet up",
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )

        made = activity_to_event_body(activity)
        linked = activity_to_event_body(activity, owns_event=False)

        assert made is not None
        assert linked is not None
        self.assertEqual(made["location"], "")
        self.assertNotIn("location", linked)


class RateLimitRefusalTests(SimpleTestCase):
    """Which Google refusals are rate or usage limits, from the bodies Google sends."""

    def test_rate_and_usage_limits_are_recognised(self) -> None:
        for label, (status, payload) in _RATE_LIMITS.items():
            with self.subTest(label):
                self.assertTrue(is_rate_limit_refusal(_raw_response(status, json.dumps(payload).encode())))

    def test_auth_and_permission_refusals_are_not(self) -> None:
        for label, (status, payload) in _DEAD_GRANTS.items():
            with self.subTest(label):
                self.assertFalse(is_rate_limit_refusal(_raw_response(status, json.dumps(payload).encode())))

    def test_a_403_without_a_readable_reason_is_not(self) -> None:
        for content in (
            b"",
            b"<html><body>403 Forbidden</body></html>",
            b"[]",
            b'{"error": "forbidden"}',
            b'{"error": {"errors": "x"}}',
            b'{"error": {"errors": [{"reason": ["rateLimitExceeded"]}]}}',
        ):
            with self.subTest(content=content):
                self.assertFalse(is_rate_limit_refusal(_raw_response(403, content)))

    def test_a_disabled_api_shares_the_usage_limits_domain_but_is_not_a_rate_limit(self) -> None:
        message = (
            "Access Not Configured. Google Calendar API has not been used in project 123456789012 before or it is "
            "disabled."
        )
        body = _google_error(403, "accessNotConfigured", message)

        self.assertFalse(is_rate_limit_refusal(_raw_response(403, json.dumps(body).encode())))


class GoogleRateLimitIsNotADeadGrantTests(_TripWithAMateCase):
    def _refuse_from(self, request_number: int, status: int, payload: dict[str, Any]) -> None:
        """Answer every request after *request_number* with this refusal."""
        self.google.fail_after = request_number
        self.google.failure = (status, payload)

    def _connected(self) -> bool:
        return GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists()

    def test_the_gateway_raises_a_transient_refusal_for_each_rate_or_usage_limit(self) -> None:
        for label, (status, payload) in _RATE_LIMITS.items():
            with self.subTest(label):
                self._refuse_from(0, status, payload)
                with self.assertRaises(CalendarRateLimitedError) as caught:
                    GoogleCalendarGateway(account=self.account).update_event("evt", {"summary": "x"})
                self.assertIsInstance(caught.exception, RateLimitExceededError)
                self.assertNotIsInstance(caught.exception, GoogleAuthExpiredError)

    def test_the_gateway_still_reads_an_auth_or_permission_refusal_as_a_dead_grant(self) -> None:
        for label, (status, payload) in _DEAD_GRANTS.items():
            with self.subTest(label):
                self._refuse_from(0, status, payload)
                with self.assertRaises(GoogleAuthExpiredError):
                    GoogleCalendarGateway(account=self.account).update_event("evt", {"summary": "x"})

    def test_an_export_cut_short_by_a_rate_limit_is_finished_by_a_push(self) -> None:
        for minute in range(3):
            TripActivity.objects.create(
                trip=self.trip,
                added_by=self.profile,
                title=f"Stop {minute}",
                scheduled_at=datetime.datetime(2026, 11, 6, 9, minute, tzinfo=datetime.UTC),
            )
        self._refuse_from(2, 403, USER_RATE_LIMIT)

        result = export_trip_to_calendar(self.account, self.trip)

        self.assertFalse(result.complete)
        self.assertEqual((result.events_synced, result.events_total), (2, 4))
        self.assertTrue(self._connected())
        self.assertIsNotNone(self._link(None).push_requested_at)

        self.google.fail_after = None
        self._age_push_request(self.trip)
        self.assertEqual(push_trip_to_calendar(self.trip.pk), 1)
        self.assertEqual(len(self.google.live_events()), 4)

    def test_a_push_refused_for_a_rate_limit_stays_owed_and_one_refused_for_scope_does_not(self) -> None:
        export_trip_to_calendar(self.account, self.trip)
        TripCalendarLink.objects.filter(pk=self._link(None).pk).update(auto_sync=True)
        cases = (("rate limit", RATE_LIMIT, True), ("scope", INSUFFICIENT_SCOPE, False))
        for label, payload, owed in cases:
            with self.subTest(label):
                Trip.objects.filter(pk=self.trip.pk).update(name=f"Renamed for {label}")
                self.trip.refresh_from_db()
                self.trip.save(update_fields=["updated"])
                self._refuse_from(len(self.google.requests), 403, payload)

                self.assertEqual(push_auto_synced_trip_changes(self.trip), 0)

                self.assertEqual(self._link(None).push_requested_at is not None, owed)
                self.assertTrue(self._connected())

    def test_the_export_button_says_busy_and_keeps_the_connection(self) -> None:
        self._refuse_from(0, 403, RATE_LIMIT)
        self.client.force_login(self.user)

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(
            json.loads(response["HX-Trigger"])["showToast"]["message"], calendar_sync.CALENDAR_BUSY_MESSAGE
        )
        self.assertTrue(self._connected())

    def test_the_export_button_still_drops_a_connection_google_revoked(self) -> None:
        self._refuse_from(0, 403, INSUFFICIENT_SCOPE)
        self.client.force_login(self.user)

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))

        self.assertIn("reconnect", json.loads(response["HX-Trigger"])["showToast"]["message"])
        self.assertFalse(self._connected())

    def test_the_api_answers_503_and_keeps_the_connection(self) -> None:
        self._refuse_from(0, 429, TOO_MANY_REQUESTS)
        api_key, raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]
        )

        response = self.client.post(
            reverse("external_api:trips.calendar_export", args=[self.trip.slug]),
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        self.assertTrue(self._connected())

    def test_the_import_dialog_keeps_the_connection(self) -> None:
        self._refuse_from(0, 403, USAGE_LIMIT)
        self.client.force_login(self.user)

        response = self.client.get(reverse("trips.calendar.import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be reached")
        self.assertTrue(self._connected())
