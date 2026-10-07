"""A location hidden after export does not stay on a calendar UrbanLens can still reach (P336).

Three routes left it there. A change of what a member may see that did not save the trip queued no push, so an
auto-synced calendar kept the address until the trip next changed. A deleted stop took its link with it and left its
event on every exporter's calendar. And an export without auto-sync is never pushed, so what 0.8.0 wrote stays until
its owner exports again; ``manage.py clear_withheld_calendar_locations`` rewrites those events once, at the 0.9.0
rollout. Every write goes through the calendar gateway and its budget, against ``FakeGoogleCalendar``.
"""

from __future__ import annotations

import datetime
from io import StringIO
import json
import os
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.utils import timezone

from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarEventDeletion,
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import TripActivity
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.services.social.friendship import remove_friend
from urbanlens.dashboard.services.trips.calendar_sync import export_trip_to_calendar
from urbanlens.dashboard.services.trips.trip_activities import delete_activity, update_activity
from urbanlens.dashboard.services.trips.trip_crud import delete_trip
from urbanlens.dashboard.services.trips.trip_visibility import HIDDEN_ACTIVITY_TITLE
from urbanlens.dashboard.tasks import (
    MAX_CALENDAR_PUSH_ATTEMPTS,
    delete_orphaned_calendar_events,
    push_trip_to_calendar,
    requeue_pending_calendar_pushes,
)
from urbanlens.dashboard.tests.hypothesis.test_calendar_withheld_fields import _ADDRESS, _TripWithAMateCase

_COMMAND = "clear_withheld_calendar_locations"


class _ExportedStopCase(_TripWithAMateCase):
    """The mate's stop, visible to the exporter, is on the exporter's calendar."""

    def setUp(self) -> None:
        super().setUp()
        self._set_mate_visibility(VisibilityChoice.ANYONE)
        self.activity = self._located_activity(added_by=self.mate)
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(self.activity)["location"], _ADDRESS)
        self.enqueue.reset_mock()

    def _auto_sync(self) -> None:
        TripCalendarLink.objects.filter(pk=self._link(None).pk).update(auto_sync=True)

    def _pushes_queued(self) -> list[int]:
        return [call.args[1] for call in self.enqueue.call_args_list if call.args[0] is push_trip_to_calendar]

    def _deliver(self) -> None:
        for trip_id in self._pushes_queued():
            push_trip_to_calendar(trip_id)

    def _assert_cleared(self) -> None:
        for event in (self._event(self.activity), self._event(None)):
            self.assertEqual(event["location"], "")
            self._assert_nothing_names_the_place(event)
        self.assertEqual(self._event(self.activity)["summary"], f"Long weekend: {HIDDEN_ACTIVITY_TITLE}")


class AVisibilityChangeQueuesThePushAnEditWouldTests(_ExportedStopCase):
    def test_hiding_the_stop_queues_the_push_that_clears_it(self) -> None:
        self._auto_sync()

        with self.captureOnCommitCallbacks(execute=True):
            update_activity(self.trip, self.mate, self.activity.pk, changes={"location_hidden": True})

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_a_trip_mate_restricting_their_setting_queues_the_push(self) -> None:
        self._auto_sync()
        self.mate.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            self.mate.trip_pin_location_visibility = VisibilityChoice.NO_ONE
            self.mate.save()

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_a_trip_mate_turning_community_off_queues_the_push(self) -> None:
        """Community off forces every visibility to NO_ONE in ``Profile.save``."""
        self._auto_sync()
        Profile.objects.filter(pk=self.mate.pk).update(community_enabled=True)
        self.mate.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            self.mate.community_enabled = False
            self.mate.save(update_fields=["community_enabled"])

        self.assertEqual(self._pushes_queued(), [self.trip.pk])

    def test_a_restriction_restored_from_a_data_import_queues_the_push(self) -> None:
        self._auto_sync()
        with tempfile.TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "settings.json"), "w", encoding="utf-8") as handle:
                json.dump({"privacy": {"trip_pin_location_visibility": VisibilityChoice.NO_ONE}}, handle)

            with self.captureOnCommitCallbacks(execute=True):
                import_data._import_settings(
                    self.mate, data_dir, import_data.ImportResult(), pin_uuid_map={}, label_uuid_map={}
                )

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_ending_the_friendship_a_friends_only_stop_relied_on_queues_the_push(self) -> None:
        self._set_mate_visibility(VisibilityChoice.FRIENDS)
        Friendship.objects.create(from_profile=self.mate, to_profile=self.profile, status=FriendshipStatus.ACCEPTED)
        TripCalendarLink.objects.filter(trip=self.trip, profile=self.profile).update(event_fingerprint="")
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(self.activity)["location"], _ADDRESS)
        self._auto_sync()
        self.enqueue.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            remove_friend(self.mate, self.profile)

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_removing_the_pin_a_common_pin_stop_relied_on_queues_the_push(self) -> None:
        self._set_mate_visibility(VisibilityChoice.COMMON_PIN)
        pin = Pin.objects.create(profile=self.profile, location=self.activity.location)
        TripCalendarLink.objects.filter(trip=self.trip, profile=self.profile).update(event_fingerprint="")
        export_trip_to_calendar(self.account, self.trip)
        self.assertEqual(self._event(self.activity)["location"], _ADDRESS)
        self._auto_sync()
        self.enqueue.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            pin.delete()

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_the_adder_deleting_their_account_queues_the_push(self) -> None:
        """Their stops stay, with no adder, and a stop with no adder is hidden from everyone."""
        self._auto_sync()

        with self.captureOnCommitCallbacks(execute=True):
            User.objects.filter(pk=self.mate.user_id).delete()

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self._assert_cleared()

    def test_deleting_a_stop_pushes_the_trip_event_it_located(self) -> None:
        self._auto_sync()
        trip_event = self._link(None).google_event_id

        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.mate, self.activity.pk)

        self.assertEqual(self._pushes_queued(), [self.trip.pk])
        self._deliver()
        self.assertEqual(self.google.events[trip_event]["location"], "")

    def test_a_save_that_changes_no_visibility_queues_nothing(self) -> None:
        self._auto_sync()
        self.mate.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            self.mate.save()
            Profile.objects.get(pk=self.mate.pk).save(update_fields=["updated"])

        self.assertEqual(self._pushes_queued(), [])

    def test_an_export_without_auto_sync_is_not_pushed_by_any_of_them(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.mate.trip_pin_location_visibility = VisibilityChoice.NO_ONE
            self.mate.save()
            update_activity(self.trip, self.mate, self.activity.pk, changes={"location_hidden": True})

        self.assertEqual(self._pushes_queued(), [])
        self.assertIsNone(self._link(None).push_requested_at)
        self.assertEqual(self._event(self.activity)["location"], _ADDRESS)


class ADeletedStopTakesItsEventWithItTests(_ExportedStopCase):
    def _deletions_queued(self) -> list[int]:
        return [call.args[1] for call in self.enqueue.call_args_list if call.args[0] is delete_orphaned_calendar_events]

    def test_deleting_an_exported_stop_deletes_its_event_through_the_queue(self) -> None:
        event_id = self._link(self.activity).google_event_id

        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, self.activity.pk)

        self.assertEqual(self._deletions_queued(), [self.profile.pk])
        self.assertEqual(list(CalendarEventDeletion.objects.values_list("google_event_id", flat=True)), [event_id])
        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 1)
        self.assertEqual(self.google.events[event_id]["status"], "cancelled")
        self.assertFalse(CalendarEventDeletion.objects.exists())
        self.assertEqual(self.google.requests[-1][:2], ("DELETE", event_id))

    def test_deleting_the_trip_deletes_every_event_it_made_once(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            delete_trip(self.trip, self.profile)

        self.assertEqual(self._deletions_queued(), [self.profile.pk])
        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 2)
        self.assertEqual(self.google.live_events(), [])

    def test_an_event_an_import_linked_from_the_users_own_calendar_is_never_deleted(self) -> None:
        self.google.events["users-own"] = {"id": "users-own", "status": "confirmed", "summary": "Dentist"}
        own = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Dentist",
            scheduled_at=datetime.datetime(2026, 11, 7, 9, 0, tzinfo=datetime.UTC),
        )
        TripCalendarLink.objects.create(
            trip=self.trip,
            activity=own,
            profile=self.profile,
            google_event_id="users-own",
            direction=CalendarSyncDirection.IMPORTED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, own.pk)

        self.assertFalse(CalendarEventDeletion.objects.exists())
        self.assertEqual(self.google.events["users-own"]["status"], "confirmed")

    def test_an_event_already_gone_is_settled(self) -> None:
        event_id = self._link(self.activity).google_event_id
        self.google.events.pop(event_id)

        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, self.activity.pk)

        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 1)
        self.assertFalse(CalendarEventDeletion.objects.exists())

    def test_a_delete_the_budget_refuses_waits_for_the_sweep(self) -> None:
        event_id = self._link(self.activity).google_event_id
        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, self.activity.pk)
        self.google.refuse_next = [
            (
                429,
                {"error": {"code": 429, "message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}},
            )
        ]

        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 0)
        self.assertEqual(CalendarEventDeletion.objects.get().attempts, 0)
        self.assertTrue(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())

        CalendarEventDeletion.objects.update(created=timezone.now() - datetime.timedelta(hours=1))
        self.enqueue.reset_mock()
        requeue_pending_calendar_pushes()
        self.assertEqual(self._deletions_queued(), [self.profile.pk])
        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 1)
        self.assertEqual(self.google.events[event_id]["status"], "cancelled")

    def test_without_a_connection_the_delete_waits_and_a_reconnect_delivers_it(self) -> None:
        event_id = self._link(self.activity).google_event_id
        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, self.activity.pk)
        self.account.delete()
        sent = len(self.google.requests)

        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 0)
        self.assertEqual(len(self.google.requests), sent)

        GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="a",
            refresh_token="r",
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )  # noqa: S106 - fixture value
        self.assertEqual(delete_orphaned_calendar_events(self.profile.pk), 1)
        self.assertEqual(self.google.events[event_id]["status"], "cancelled")

    def test_the_sweep_drops_a_delete_that_keeps_failing_or_is_too_old(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            delete_activity(self.trip, self.profile, self.activity.pk)
        CalendarEventDeletion.objects.update(attempts=MAX_CALENDAR_PUSH_ATTEMPTS)

        requeue_pending_calendar_pushes()

        self.assertFalse(CalendarEventDeletion.objects.exists())

    def test_a_departing_member_whose_delete_fails_has_it_queued_and_keeps_an_imported_event(self) -> None:
        from urbanlens.dashboard.services.trips.calendar_sync import disconnect_member_calendar_sync

        TripCalendarLink.objects.filter(pk=self._link(None).pk).update(
            direction=CalendarSyncDirection.IMPORTED, google_event_id="users-trip-event"
        )
        self.google.events["users-trip-event"] = {"id": "users-trip-event", "status": "confirmed"}
        activity_event = self._link(self.activity).google_event_id
        self.google.refuse_next = [(500, {"error": {"message": "backend error"}})]

        disconnect_member_calendar_sync(self.trip, self.profile)

        self.assertEqual(self.google.events["users-trip-event"]["status"], "confirmed")
        self.assertEqual(
            list(CalendarEventDeletion.objects.values_list("google_event_id", flat=True)), [activity_event]
        )


class ClearWithheldCalendarLocationsTests(_ExportedStopCase):
    """The rollout command, for exports nothing pushes: dry-run by default, ``--apply`` to write, resumable."""

    def setUp(self) -> None:
        super().setUp()
        self.quiet_activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Lunch",
            scheduled_at=datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.UTC),
        )
        export_trip_to_calendar(self.account, self.trip)
        self._set_mate_visibility(VisibilityChoice.NO_ONE)
        # 0067 blanks every fingerprint on the upgrade from 0.8.0.
        TripCalendarLink.objects.update(event_fingerprint="")

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command(_COMMAND, *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_reports_what_it_would_clear_and_writes_nothing(self) -> None:
        sent = len(self.google.requests)

        output = self._run()

        self.assertIn("2 events", output)
        self.assertEqual(len(self.google.requests), sent)
        self.assertEqual(self._event(self.activity)["location"], _ADDRESS)

    def test_apply_clears_the_withheld_location_and_title_and_nothing_else(self) -> None:
        sent = len(self.google.requests)

        self._run("--apply")

        self._assert_cleared()
        written = [event_id for method, event_id, _body in self.google.requests[sent:]]
        self.assertEqual(
            sorted(written), sorted([self._link(self.activity).google_event_id, self._link(None).google_event_id])
        )
        self.assertEqual({method for method, _id, _body in self.google.requests[sent:]}, {"PATCH"})
        self.assertEqual(self._link(self.quiet_activity).event_fingerprint, "")
        self.assertIn("0 events", self._run())

    def test_it_stops_when_the_budget_runs_out_and_a_second_run_finishes(self) -> None:
        self._limit(1)
        from urbanlens.dashboard.models.api_call_log import ApiCallLog

        ApiCallLog.objects.filter(service="google_calendar").delete()
        sent = len(self.google.requests)

        first = self._run("--apply", "--no-wait")

        self.assertIn("budget", first)
        self.assertEqual(len(self.google.requests) - sent, 1)
        self._let_the_minute_pass()
        self._run("--apply", "--no-wait")
        self._assert_cleared()
        self.assertEqual(len(self.google.requests) - sent, 2)

    def test_waiting_out_the_budget_finishes_in_one_run(self) -> None:
        self._limit(1)
        from urbanlens.dashboard.models.api_call_log import ApiCallLog

        ApiCallLog.objects.filter(service="google_calendar").delete()

        with mock.patch(
            "urbanlens.dashboard.management.commands.clear_withheld_calendar_locations.time.sleep",
            side_effect=lambda _seconds: self._let_the_minute_pass(),
        ) as slept:
            self._run("--apply")

        self.assertTrue(slept.called)
        self._assert_cleared()

    def test_an_event_an_import_linked_is_left_alone(self) -> None:
        """Only events UrbanLens made: a user's own event keeps whatever it holds."""
        TripCalendarLink.objects.filter(pk=self._link(None).pk).update(
            direction=CalendarSyncDirection.IMPORTED, google_event_id="users-trip-event"
        )
        self.google.events["users-trip-event"] = {"id": "users-trip-event", "status": "confirmed", "location": _ADDRESS}
        sent = len(self.google.requests)

        self._run("--apply")

        self.assertEqual(self.google.events["users-trip-event"]["location"], _ADDRESS)
        self.assertEqual(
            [event_id for _m, event_id, _b in self.google.requests[sent:]], [self._link(self.activity).google_event_id]
        )

    def test_an_event_the_user_deleted_is_not_recreated(self) -> None:
        event_id = self._link(self.activity).google_event_id
        self.google.events.pop(event_id)
        sent = len(self.google.requests)

        output = self._run("--apply")

        self.assertNotIn(event_id, self.google.events)
        self.assertNotIn("POST", {method for method, _id, _body in self.google.requests[sent:]})
        self.assertIn("gone", output)
