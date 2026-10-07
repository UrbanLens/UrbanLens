"""A user's own calendar event is never deleted, a refused event does not stop a push, and the rollout command reaches
every event that may hold a withheld location.

An import links the user's own event; only an event UrbanLens made may be deleted, on every removal path. See
UrbanLens#330 ("removing an imported trip deletes the user's own event"). A push writes every event it can, and stops
early only for a refusal that would refuse the rest too. See UrbanLens#332 ("a push stops at the first refused
event"). ``clear_withheld_calendar_locations`` also rewrites an unscheduled stop's event and an imported event whose
fingerprint shows UrbanLens wrote the location now withheld. See UrbanLens#333 ("the rollout command misses
unscheduled and imported events").
"""

from __future__ import annotations

import datetime
from io import StringIO
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarEventDeletion,
    CalendarSyncDirection,
    TripCalendarLink,
)
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.services.apis.calendar.google import CalendarEventNotFoundError, GoogleCalendarGateway
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.core.rate_limiter import RateLimiterUnavailableError, ServiceDisabledError
from urbanlens.dashboard.services.trips.calendar_sync import (
    export_trip_to_calendar,
    push_auto_synced_trip_changes,
    queue_calendar_event_deletion,
    remove_trip_from_calendar,
)
from urbanlens.dashboard.services.trips.trip_crud import delete_trip
from urbanlens.dashboard.services.trips.trip_visibility import HIDDEN_ACTIVITY_TITLE
from urbanlens.dashboard.tests.hypothesis.test_calendar_transient_failures import NON_ORGANIZER
from urbanlens.dashboard.tests.hypothesis.test_calendar_withheld_fields import _ADDRESS, TOO_MANY_REQUESTS
from urbanlens.dashboard.tests.hypothesis.test_calendar_withheld_reach import _COMMAND, _ExportedStopCase

_USERS_TRIP_EVENT = "users-own-trip"
_USERS_TIMED_EVENT = "users-own-timed"


class _ImportedCase(_ExportedStopCase):
    """The exporter's trip and stop events, either of which a test can turn into one an import linked."""

    def _import_trip_event(self, *, location: str = "Our cabin") -> None:
        """Make the trip's own event the user's, as an all-day import leaves it."""
        TripCalendarLink.objects.filter(pk=self._link(None).pk).update(
            direction=CalendarSyncDirection.IMPORTED, google_event_id=_USERS_TRIP_EVENT, event_fingerprint=""
        )
        self.google.events[_USERS_TRIP_EVENT] = {"id": _USERS_TRIP_EVENT, "status": "confirmed", "location": location}

    def _import_stop_event(self) -> None:
        """Make the stop's event the user's, as a timed import leaves it."""
        TripCalendarLink.objects.filter(pk=self._link(self.activity).pk).update(
            direction=CalendarSyncDirection.IMPORTED, google_event_id=_USERS_TIMED_EVENT, event_fingerprint=""
        )
        self.google.events[_USERS_TIMED_EVENT] = {"id": _USERS_TIMED_EVENT, "status": "confirmed"}

    def _deleted(self) -> set[str | None]:
        return {event_id for method, event_id, _body in self.google.requests if method == "DELETE"}

    def _api(self, method: str) -> dict:
        api_key, raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]
        )
        url = reverse("external_api:trips.calendar_export", args=[self.trip.slug])
        send = self.client.delete if method == "DELETE" else self.client.post
        response = send(url, {}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _press(self, method: str) -> str:
        self.client.force_login(self.user)
        url = reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug})
        response = self.client.delete(url) if method == "DELETE" else self.client.post(url)
        return json.loads(response["HX-Trigger"])["showToast"]["message"]


class AUsersOwnEventIsNeverDeletedTests(_ImportedCase):
    def test_removing_the_trip_deletes_what_urbanlens_made_and_only_unlinks_the_users_own(self) -> None:
        self._import_trip_event()
        activity_event = self._link(self.activity).google_event_id

        removal = remove_trip_from_calendar(self.account, self.trip)

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["status"], "confirmed")
        self.assertEqual(self.google.events[activity_event]["status"], "cancelled")
        self.assertEqual(self._deleted(), {activity_event})
        self.assertFalse(TripCalendarLink.objects.filter(trip=self.trip, profile=self.profile).exists())
        self.assertEqual((removal.unlinked, removal.deleted, removal.kept), (2, 1, 1))

    def test_a_timed_imports_own_event_is_unlinked_not_deleted(self) -> None:
        self._import_stop_event()

        remove_trip_from_calendar(self.account, self.trip)

        self.assertEqual(self.google.events[_USERS_TIMED_EVENT]["status"], "confirmed")
        self.assertNotIn(_USERS_TIMED_EVENT, self._deleted())

    def test_the_button_and_the_api_say_the_users_own_event_stays(self) -> None:
        self._import_trip_event()

        message = self._press("DELETE")

        self.assertIn("imported", message)
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["status"], "confirmed")

        export_trip_to_calendar(self.account, self.trip)
        self._import_trip_event()
        payload = self._api("DELETE")
        self.assertEqual((payload["removed"], payload["events_kept"]), (True, 1))
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["status"], "confirmed")

    def test_an_imported_stop_that_loses_its_schedule_keeps_its_own_event(self) -> None:
        self._import_stop_event()
        link = self._link(self.activity)
        TripActivity.objects.filter(pk=self.activity.pk).update(scheduled_at=None)

        result = export_trip_to_calendar(self.account, self.trip)

        self.assertEqual(self.google.events[_USERS_TIMED_EVENT]["status"], "confirmed")
        self.assertNotIn(_USERS_TIMED_EVENT, self._deleted())
        self.assertFalse(TripCalendarLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(result.complete)

    def test_a_stop_urbanlens_made_an_event_for_still_loses_it_with_its_schedule(self) -> None:
        """Anti-vacuity: the unscheduled path still deletes what UrbanLens made."""
        event_id = self._link(self.activity).google_event_id
        TripActivity.objects.filter(pk=self.activity.pk).update(scheduled_at=None)

        export_trip_to_calendar(self.account, self.trip)

        self.assertEqual(self.google.events[event_id]["status"], "cancelled")

    def test_deleting_the_trip_queues_deletes_only_for_what_urbanlens_made(self) -> None:
        self._import_trip_event()
        activity_event = self._link(self.activity).google_event_id

        with self.captureOnCommitCallbacks(execute=True):
            delete_trip(self.trip, self.profile)

        self.assertEqual(
            list(CalendarEventDeletion.objects.values_list("google_event_id", flat=True)), [activity_event]
        )

    def test_no_delete_is_ever_queued_for_an_event_an_import_linked(self) -> None:
        self._import_trip_event()

        queue_calendar_event_deletion(self._link(None), trip_uuid=self.trip.uuid)

        self.assertFalse(CalendarEventDeletion.objects.exists())

    def test_deleting_the_account_deletes_no_event(self) -> None:
        self._import_trip_event()
        sent = len(self.google.requests)

        with self.captureOnCommitCallbacks(execute=True):
            User.objects.filter(pk=self.user.pk).delete()

        self.assertEqual(len(self.google.requests), sent)
        self.assertFalse(CalendarEventDeletion.objects.exists())


class APushGoesOnPastAnEventGoogleRefusesTests(_ImportedCase):
    def _restrict_the_mate(self) -> None:
        self.mate.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.mate.trip_pin_location_visibility = VisibilityChoice.NO_ONE
            self.mate.save()

    def test_a_refused_event_does_not_keep_a_withheld_location_on_the_events_after_it(self) -> None:
        self._auto_sync()
        self.google.refuse_events[self._link(None).google_event_id] = (403, NON_ORGANIZER)

        self._restrict_the_mate()
        self._deliver()

        self.assertEqual(self._event(self.activity)["location"], "")
        self.assertEqual(self._event(self.activity)["summary"], f"Long weekend: {HIDDEN_ACTIVITY_TITLE}")
        self.assertIsNotNone(self._link(None).push_requested_at)

    def test_the_refusal_counts_toward_the_cap_only_once_nothing_else_is_written(self) -> None:
        self._auto_sync()
        self.google.refuse_events[self._link(None).google_event_id] = (403, NON_ORGANIZER)
        self._restrict_the_mate()

        push_auto_synced_trip_changes(self.trip)
        self.assertEqual(self._link(None).push_attempts, 0)

        push_auto_synced_trip_changes(self.trip)
        self.assertEqual(self._link(None).push_attempts, 1)

    def test_the_button_reports_n_of_m_and_does_not_promise_the_refused_event(self) -> None:
        TripCalendarLink.objects.filter(trip=self.trip).update(event_fingerprint="")
        self.google.refuse_events[self._link(self.activity).google_event_id] = (403, NON_ORGANIZER)

        message = self._press("POST")

        self.assertIn("1 of 2 events", message)
        self.assertIn("refused", message)
        self.assertNotIn("follow automatically", message)

    def test_the_api_reports_the_refused_events(self) -> None:
        TripCalendarLink.objects.filter(trip=self.trip).update(event_fingerprint="")
        self.google.refuse_events[self._link(self.activity).google_event_id] = (403, NON_ORGANIZER)

        payload = self._api("POST")

        self.assertEqual(
            (payload["complete"], payload["events_synced"], payload["events_total"], payload["events_refused"]),
            (False, 1, 2, 1),
        )

    def test_a_failure_that_is_not_google_refusing_the_event_still_ends_the_attempt(self) -> None:
        """Our limiter being unreadable, the service switched off, or the calendar gone would refuse every write."""
        TripCalendarLink.objects.filter(trip=self.trip).update(event_fingerprint="")
        failures = {
            "limiter unreadable": RateLimiterUnavailableError("google_calendar"),
            "service off": ServiceDisabledError("google_calendar"),
        }
        for label, failure in failures.items():
            with (
                self.subTest(label),
                mock.patch.object(GoogleCalendarGateway, "update_event", side_effect=failure),
                self.assertRaises(type(failure)),
            ):
                export_trip_to_calendar(self.account, self.trip)
        sent = len(self.google.requests)
        self.google.fail_after, self.google.failure = sent, (404, {"error": {"message": "Not Found"}})

        with self.assertRaises(CalendarEventNotFoundError):
            export_trip_to_calendar(self.account, self.trip)

        # The trip's update, then its create: nothing more once the calendar is plainly gone.
        self.assertEqual(len(self.google.requests) - sent, 2)

    def test_a_push_our_limiter_could_not_read_for_is_not_counted(self) -> None:
        self._auto_sync()
        TripCalendarLink.objects.filter(trip=self.trip).update(event_fingerprint="", push_requested_at=timezone.now())

        with mock.patch.object(
            GoogleCalendarGateway, "update_event", side_effect=RateLimiterUnavailableError("google_calendar")
        ):
            push_auto_synced_trip_changes(self.trip)

        link = self._link(None)
        self.assertEqual(link.push_attempts, 0)
        self.assertIsNotNone(link.push_requested_at)

    def test_a_rate_limit_still_ends_the_attempt(self) -> None:
        """Going on would only spend the budget on refusals."""
        TripCalendarLink.objects.filter(trip=self.trip).update(event_fingerprint="")
        sent = len(self.google.requests)
        self.google.refuse_next = [(429, TOO_MANY_REQUESTS)]

        result = export_trip_to_calendar(self.account, self.trip)

        self.assertFalse(result.complete)
        self.assertEqual(len(self.google.requests) - sent, 1)

    def test_with_budget_for_only_some_events_the_withheld_ones_are_written_first(self) -> None:
        own = [
            TripActivity.objects.create(
                trip=self.trip,
                added_by=self.profile,
                title=f"Own stop {hour}",
                scheduled_at=datetime.datetime(2026, 11, 6, hour, 0, tzinfo=datetime.UTC),
            )
            for hour in (6, 7, 8)
        ]
        export_trip_to_calendar(self.account, self.trip)
        self._let_the_minute_pass()
        self._limit(2)
        Trip.objects.filter(pk=self.trip.pk).update(name="Renamed weekend")
        self.trip.refresh_from_db()
        self._set_mate_visibility(VisibilityChoice.NO_ONE)

        result = export_trip_to_calendar(self.account, self.trip)

        self.assertFalse(result.complete)
        self.assertEqual(self._event(self.activity)["location"], "")
        self.assertEqual(self._event(self.activity)["summary"], f"Renamed weekend: {HIDDEN_ACTIVITY_TITLE}")
        self.assertTrue(all(self._event(stop)["summary"].startswith("Long weekend") for stop in own))


class TheRolloutCommandReachesEveryEventThatMayHoldAWithheldLocationTests(_ImportedCase):
    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command(_COMMAND, *args, stdout=out)
        return out.getvalue()

    def _upgrade_from_0_8_0(self) -> None:
        """0067 blanks every fingerprint on the upgrade."""
        TripCalendarLink.objects.update(event_fingerprint="")

    def test_an_unscheduled_stops_event_has_its_withheld_location_and_title_cleared_not_deleted(self) -> None:
        event_id = self._link(self.activity).google_event_id
        TripActivity.objects.filter(pk=self.activity.pk).update(scheduled_at=None)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        self._upgrade_from_0_8_0()

        self.assertIn("2 events", self._run())
        self._run("--apply")

        event = self.google.events[event_id]
        self.assertEqual((event["status"], event["location"]), ("confirmed", ""))
        self.assertEqual(event["summary"], f"Long weekend: {HIDDEN_ACTIVITY_TITLE}")
        self._assert_nothing_names_the_place(event)
        self.assertIn("0 events", self._run())

    def test_an_imported_event_urbanlens_provably_wrote_the_withheld_location_onto_is_cleared(self) -> None:
        self._import_trip_event()
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], _ADDRESS)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)

        self._run("--apply")

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], "")
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["status"], "confirmed")

    def _imported_with_proof(self) -> None:
        """An all-day import whose own event the export then wrote the mate's address onto, recording a fingerprint."""
        self._import_trip_event()
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], _ADDRESS)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)

    def test_a_cleared_imported_event_is_not_counted_again(self) -> None:
        self._imported_with_proof()
        self._run("--apply")

        output = self._run("--apply")

        self.assertIn("Rewrote 0 events", output)
        self.assertNotIn("imported events alone", output)

    def test_an_imported_event_google_refused_once_is_cleared_by_the_next_run(self) -> None:
        self._imported_with_proof()
        self.google.refuse_events[_USERS_TRIP_EVENT] = (403, NON_ORGANIZER)
        self._run("--apply")
        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], _ADDRESS)
        del self.google.refuse_events[_USERS_TRIP_EVENT]

        self._run("--apply")

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], "")

    def test_an_imported_event_the_budget_stopped_is_cleared_by_the_next_run(self) -> None:
        self._imported_with_proof()
        self.google.refuse_events[_USERS_TRIP_EVENT] = (429, TOO_MANY_REQUESTS)
        self.assertIn("budget", self._run("--apply", "--no-wait"))
        del self.google.refuse_events[_USERS_TRIP_EVENT]

        self._run("--apply", "--no-wait")

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], "")

    def test_clearing_an_imported_event_leaves_what_the_user_changed_since(self) -> None:
        self._imported_with_proof()
        self.google.events[_USERS_TRIP_EVENT].update(description="My own notes", start={"date": "2030-01-01"})

        self._run("--apply")

        event = self.google.events[_USERS_TRIP_EVENT]
        self.assertEqual(event["location"], "")
        self.assertEqual((event["description"], event["start"]), ("My own notes", {"date": "2030-01-01"}))

    def test_an_imported_location_the_user_changed_since_is_theirs(self) -> None:
        self._imported_with_proof()
        self.google.events[_USERS_TRIP_EVENT]["location"] = "My own new place"

        self._run("--apply")

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], "My own new place")

    def test_an_imported_event_with_no_record_of_what_urbanlens_wrote_is_left_alone_and_reported(self) -> None:
        self._import_trip_event()
        export_trip_to_calendar(self.account, self.trip)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        self._upgrade_from_0_8_0()

        output = self._run("--apply")

        self.assertEqual(self.google.events[_USERS_TRIP_EVENT]["location"], _ADDRESS)
        self.assertIn("Left 1 imported events alone", output)

    def test_an_imported_event_the_withholding_does_not_touch_is_neither_written_nor_reported(self) -> None:
        """No located stop is the user's or reaches the trip's own event: there is nothing UrbanLens could have put there."""
        self._import_trip_event()
        TripActivity.objects.filter(pk=self.activity.pk).update(location=None)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        self._upgrade_from_0_8_0()
        sent = len(self.google.requests)

        output = self._run("--apply")

        self.assertNotIn(_USERS_TRIP_EVENT, {event_id for _m, event_id, _b in self.google.requests[sent:]})
        self.assertNotIn("imported events alone", output)

    def test_an_event_google_refuses_does_not_stop_the_rest(self) -> None:
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        self._upgrade_from_0_8_0()
        self.google.refuse_events[self._link(None).google_event_id] = (403, NON_ORGANIZER)

        output = self._run("--apply")

        self.assertEqual(self._event(self.activity)["location"], "")
        self.assertIn("refused 1", output)
