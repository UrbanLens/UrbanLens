"""Tests for the per-user Google Calendar trip sync.

Covers:
- trip_to_event_body / event_to_trip_kwargs - pure conversion both ways,
  including the all-day exclusive-end-date convention (property-based)
- import_events_as_trips - trip/membership/link creation, dedupe, and
  skipping of events that originated as UrbanLens exports (gateway mocked)
- export_trip_to_calendar / remove_trip_from_calendar - event create vs
  update, vanished-event recreation, link bookkeeping (gateway mocked)
- OAuth callback view - rejects bad/missing state without storing tokens
"""

from __future__ import annotations

import datetime
from unittest import mock

from cryptography.fernet import InvalidToken
from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, strategies as st
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.apis.calendar.google import ACTIVITY_ID_EVENT_PROPERTY, TRIP_UUID_EVENT_PROPERTY, CalendarEventNotFoundError
from urbanlens.dashboard.services.calendar_sync import (
    DEFAULT_ACTIVITY_EVENT_DURATION,
    activity_to_event_body,
    disconnect_member_calendar_sync,
    event_originated_from_urbanlens,
    event_to_trip_kwargs,
    export_trip_to_calendar,
    import_events_as_trips,
    push_auto_synced_trip_changes,
    remove_trip_from_calendar,
    trip_to_event_body,
)
from urbanlens.dashboard.services.gateway import GatewayRequestError

_DATES = st.dates(min_value=datetime.date(1990, 1, 1), max_value=datetime.date(2100, 1, 1))


class TripToEventBodyTests(TestCase):
    """trip_to_event_body maps trips to all-day event payloads."""

    @given(start=_DATES, duration_days=st.integers(min_value=0, max_value=90))
    def test_round_trip_preserves_name_and_dates(self, start, duration_days):
        """Exporting then re-importing an event yields the original trip dates."""
        end = start + datetime.timedelta(days=duration_days)
        trip = Trip(name="Round Trip", start_date=start, end_date=end)

        body = trip_to_event_body(trip)
        recovered = event_to_trip_kwargs(body)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["name"], "Round Trip")
        self.assertEqual(recovered["start_date"], start)
        self.assertEqual(recovered["end_date"], end)

    @given(start=_DATES, duration_days=st.integers(min_value=0, max_value=90))
    def test_all_day_end_is_exclusive(self, start, duration_days):
        """Google all-day end dates are exclusive: trip end + 1 day."""
        end = start + datetime.timedelta(days=duration_days)
        trip = Trip(name="X", start_date=start, end_date=end)

        body = trip_to_event_body(trip)

        self.assertEqual(body["start"]["date"], start.isoformat())
        self.assertEqual(body["end"]["date"], (end + datetime.timedelta(days=1)).isoformat())

    def test_marks_event_with_trip_uuid(self):
        trip = Trip(name="X", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 1))
        body = trip_to_event_body(trip)
        self.assertEqual(body["extendedProperties"]["private"][TRIP_UUID_EVENT_PROPERTY], str(trip.uuid))
        self.assertTrue(event_originated_from_urbanlens(body))

    def test_appends_trip_url_to_description(self):
        trip = Trip(name="X", description="Notes.", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 1))
        body = trip_to_event_body(trip, trip_url="https://example.com/trips/abc/")
        self.assertIn("Notes.", body["description"])
        self.assertIn("https://example.com/trips/abc/", body["description"])

    def test_raises_without_dates(self):
        trip = Trip.objects.create(name="Dateless")
        with self.assertRaises(ValueError):
            trip_to_event_body(trip)

    def test_end_date_derived_from_activity_scheduled_end(self):
        """Without explicit dates, the latest activity end (not just start) sets the event's end."""
        trip = Trip.objects.create(name="Derived")
        TripActivity.objects.create(
            trip=trip,
            title="Overnight stay",
            scheduled_at=datetime.datetime(2026, 9, 4, 18, 0, tzinfo=datetime.UTC),
            scheduled_end=datetime.datetime(2026, 9, 6, 11, 0, tzinfo=datetime.UTC),
        )
        body = trip_to_event_body(trip)
        self.assertEqual(body["start"]["date"], "2026-09-04")
        # Exclusive all-day end: inclusive end (Sep 6) + 1 day.
        self.assertEqual(body["end"]["date"], "2026-09-07")

    def test_trip_event_uses_first_activity_location(self):
        """The all-day trip event carries the first shareable activity location."""
        trip = Trip.objects.create(name="Located", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 2))
        TripActivity.objects.create(
            trip=trip,
            title="Second stop",
            scheduled_at=datetime.datetime(2026, 8, 1, 15, 0, tzinfo=datetime.UTC),
            lat_override=42.1,
            lng_override=-74.2,
        )
        TripActivity.objects.create(
            trip=trip,
            title="First stop",
            scheduled_at=datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC),
            lat_override=41.5,
            lng_override=-73.9,
        )
        body = trip_to_event_body(trip)
        self.assertEqual(body["location"], "41.500000, -73.900000")

    def test_trip_event_skips_hidden_locations(self):
        """Secret activity locations never leak into the trip-level event."""
        trip = Trip.objects.create(name="Secretive", start_date=datetime.date(2026, 8, 1), end_date=datetime.date(2026, 8, 2))
        TripActivity.objects.create(
            trip=trip,
            title="Secret first stop",
            scheduled_at=datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC),
            location_hidden=True,
            lat_override=41.5,
            lng_override=-73.9,
        )
        body = trip_to_event_body(trip)
        self.assertNotIn("location", body)


class EventToTripKwargsTests(TestCase):
    """event_to_trip_kwargs parses Google event resources defensively."""

    def test_timed_event_uses_date_components(self):
        event = {
            "summary": "Explore mill",
            "start": {"dateTime": "2026-08-01T15:30:00Z"},
            "end": {"dateTime": "2026-08-01T18:00:00Z"},
        }
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["start_date"], datetime.date(2026, 8, 1))
        self.assertEqual(kwargs["end_date"], datetime.date(2026, 8, 1))

    def test_cancelled_event_rejected(self):
        event = {"status": "cancelled", "start": {"date": "2026-08-01"}, "end": {"date": "2026-08-02"}}
        self.assertIsNone(event_to_trip_kwargs(event))

    def test_missing_start_rejected(self):
        self.assertIsNone(event_to_trip_kwargs({"summary": "No dates"}))
        self.assertIsNone(event_to_trip_kwargs({"start": {"date": "not-a-date"}}))

    def test_end_before_start_clamped(self):
        event = {"start": {"date": "2026-08-10"}, "end": {"date": "2026-08-05"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["start_date"], datetime.date(2026, 8, 10))
        self.assertEqual(kwargs["end_date"], datetime.date(2026, 8, 10))

    def test_untitled_event_gets_fallback_name(self):
        event = {"start": {"date": "2026-08-01"}, "end": {"date": "2026-08-02"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(kwargs["name"], "Imported calendar event")

    def test_long_summary_truncated(self):
        event = {"summary": "x" * 600, "start": {"date": "2026-08-01"}}
        kwargs = event_to_trip_kwargs(event)
        self.assertEqual(len(kwargs["name"]), 255)


class _CalendarSyncDBTestCase(TestCase):
    """Shared fixtures: a profile with a connected calendar account."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="calendar-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )

    def _patch_gateway(self):
        """Patch the gateway class used by the sync service; returns the instance mock."""
        patcher = mock.patch("urbanlens.dashboard.services.calendar_sync.GoogleCalendarGateway")
        gateway_cls = patcher.start()
        self.addCleanup(patcher.stop)
        return gateway_cls.return_value


class ImportEventsTests(_CalendarSyncDBTestCase):
    """import_events_as_trips creates trips from the user's own events."""

    def test_import_creates_trip_membership_and_link(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt1",
            "summary": "Abandoned asylum weekend",
            "description": "Bring the wide lens.",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-07"},
        }

        created, skipped, _invited = import_events_as_trips(self.account, ["evt1"])

        self.assertEqual(len(created), 1)
        self.assertEqual(skipped, [])
        trip = created[0]
        self.assertEqual(trip.name, "Abandoned asylum weekend")
        self.assertEqual(trip.start_date, datetime.date(2026, 9, 4))
        self.assertEqual(trip.end_date, datetime.date(2026, 9, 6))  # exclusive end - 1
        self.assertEqual(trip.creator, self.profile)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=self.profile).exists())
        link = TripCalendarLink.objects.get(trip=trip, profile=self.profile)
        self.assertEqual(link.google_event_id, "evt1")
        self.assertEqual(link.direction, CalendarSyncDirection.IMPORTED)

    def test_import_skips_already_linked_event(self):
        gateway = self._patch_gateway()
        trip = Trip.objects.create(name="Existing", creator=self.profile)
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt1",
            direction=CalendarSyncDirection.IMPORTED,
        )

        created, skipped, _invited = import_events_as_trips(self.account, ["evt1"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)
        gateway.get_event.assert_not_called()

    def test_import_skips_events_exported_from_urbanlens(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt2",
            "summary": "Trip echo",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
            "extendedProperties": {"private": {TRIP_UUID_EVENT_PROPERTY: "some-uuid"}},
        }

        created, skipped, _invited = import_events_as_trips(self.account, ["evt2"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)
        self.assertFalse(Trip.objects.filter(name="Trip echo").exists())

    def test_import_skips_vanished_event(self):
        gateway = self._patch_gateway()
        gateway.get_event.side_effect = CalendarEventNotFoundError("gone")

        created, skipped, _invited = import_events_as_trips(self.account, ["evt3"])

        self.assertEqual(created, [])
        self.assertEqual(len(skipped), 1)

    def test_import_creates_activity_from_event_location(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-loc",
            "summary": "Mill scouting",
            "location": "123 Factory Rd, Utica, NY",
            "start": {"dateTime": "2026-09-04T10:00:00-04:00"},
            "end": {"dateTime": "2026-09-04T12:00:00-04:00"},
        }

        created, _skipped, _invited = import_events_as_trips(self.account, [{"event_id": "evt-loc", "create_activity": True}])

        activity = created[0].activities.get()
        self.assertEqual(activity.title, "123 Factory Rd, Utica, NY")
        self.assertEqual(activity.added_by, self.profile)
        self.assertIsNotNone(activity.scheduled_at)
        self.assertIsNotNone(activity.scheduled_end)

    def test_import_can_decline_activity_creation(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-loc2",
            "summary": "Mill scouting",
            "location": "123 Factory Rd, Utica, NY",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        }

        created, _skipped, _invited = import_events_as_trips(self.account, [{"event_id": "evt-loc2", "create_activity": False}])

        self.assertEqual(created[0].activities.count(), 0)

    def test_import_without_location_creates_no_activity(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-noloc",
            "summary": "Planning call",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        }

        created, _skipped, _invited = import_events_as_trips(self.account, [{"event_id": "evt-noloc", "create_activity": True}])

        self.assertEqual(created[0].activities.count(), 0)

    def test_import_sets_auto_sync_when_requested(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-sync",
            "summary": "Keep me synced",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        }

        created, _skipped, _invited = import_events_as_trips(self.account, [{"event_id": "evt-sync", "auto_sync": True}])

        link = TripCalendarLink.objects.get(trip=created[0], profile=self.profile)
        self.assertTrue(link.auto_sync)

    def test_import_defaults_auto_sync_to_false(self):
        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-nosync",
            "summary": "One-time import",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        }

        created, _skipped, _invited = import_events_as_trips(self.account, ["evt-nosync"])

        link = TripCalendarLink.objects.get(trip=created[0], profile=self.profile)
        self.assertFalse(link.auto_sync)

    def test_import_invites_confirmed_friends_only(self):
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
        from urbanlens.dashboard.models.notifications.meta import NotificationType
        from urbanlens.dashboard.models.notifications.model import NotificationLog

        friend = User.objects.create_user(username="cal-friend", email="friend@example.com").profile
        stranger = User.objects.create_user(username="cal-stranger", email="stranger@example.com").profile
        Friendship.objects.create(from_profile=self.profile, to_profile=friend, status=FriendshipStatus.ACCEPTED)

        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-inv",
            "summary": "Group trip",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
        }

        created, skipped, invited = import_events_as_trips(
            self.account,
            [{"event_id": "evt-inv", "invite_profile_ids": [friend.pk, stranger.pk]}],
        )

        trip = created[0]
        self.assertEqual(invited, 1)
        self.assertTrue(TripMembership.objects.filter(trip=trip, profile=friend).exists())
        self.assertFalse(TripMembership.objects.filter(trip=trip, profile=stranger).exists())
        self.assertTrue(any("not friends" in reason for reason in skipped))
        self.assertTrue(
            NotificationLog.objects.filter(profile=friend, notification_type=NotificationType.ADDED_TO_TRIP).exists(),
        )


class MatchEventAttendeesTests(_CalendarSyncDBTestCase):
    """match_event_attendees splits attendees into invitable friends and labels."""

    def test_friend_attendee_is_invitable(self):
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
        from urbanlens.dashboard.services.calendar_sync import match_event_attendees

        friend = User.objects.create_user(username="att-friend", email="att-friend@example.com").profile
        Friendship.objects.create(from_profile=self.profile, to_profile=friend, status=FriendshipStatus.ACCEPTED)

        event = {
            "attendees": [
                {"email": "att-friend@example.com", "displayName": "Att Friend"},
                {"email": "nobody@example.com", "displayName": "No Account"},
                {"email": self.user.email or "me@example.com", "self": True},
            ],
        }
        friends, others = match_event_attendees(self.profile, event)

        self.assertEqual([p.pk for p in friends], [friend.pk])
        self.assertEqual(others, ["No Account"])

    def test_non_friend_account_is_not_invitable(self):
        from urbanlens.dashboard.services.calendar_sync import match_event_attendees

        User.objects.create_user(username="att-stranger", email="att-stranger@example.com")

        event = {"attendees": [{"email": "att-stranger@example.com", "displayName": "Stranger"}]}
        friends, others = match_event_attendees(self.profile, event)

        self.assertEqual(friends, [])
        self.assertEqual(others, ["Stranger"])


class CalendarImportPreviewViewTests(_CalendarSyncDBTestCase):
    """The review step renders trip, activity, and participant details."""

    def setUp(self):
        super().setUp()
        self.user.set_password("pw")
        self.user.save()
        self.client.force_login(self.user)

    def test_preview_renders_activity_and_friend_options(self):
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        friend = User.objects.create_user(username="preview-friend", email="preview-friend@example.com").profile
        Friendship.objects.create(from_profile=self.profile, to_profile=friend, status=FriendshipStatus.ACCEPTED)

        gateway = self._patch_gateway()
        gateway.get_event.return_value = {
            "id": "evt-prev",
            "summary": "Foundry day",
            "location": "1 Iron Works Ln",
            "start": {"date": "2026-09-04"},
            "end": {"date": "2026-09-05"},
            "attendees": [
                {"email": "preview-friend@example.com", "displayName": "Preview Friend"},
                {"email": "outsider@example.com", "displayName": "Outsider"},
            ],
        }

        response = self.client.post(reverse("trips.calendar.import.preview"), {"event_ids": ["evt-prev"]})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Foundry day", content)
        self.assertIn("1 Iron Works Ln", content)
        self.assertIn('name="create_activity_evt-prev"', content)
        self.assertIn(f'name="invite_evt-prev" value="{friend.pk}"', content)
        self.assertIn("Outsider", content)

    def test_preview_requires_selection(self):
        response = self.client.post(reverse("trips.calendar.import.preview"), {})
        self.assertEqual(response.status_code, 400)

    def test_preview_marks_already_linked_events(self):
        trip = Trip.objects.create(name="Linked", creator=self.profile)
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-linked",
            direction=CalendarSyncDirection.IMPORTED,
        )
        self._patch_gateway()

        response = self.client.post(reverse("trips.calendar.import.preview"), {"event_ids": ["evt-linked"]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Already linked to a trip.", response.content.decode())


class ExportTripTests(_CalendarSyncDBTestCase):
    """export_trip_to_calendar mirrors trips onto the user's own calendar."""

    def _trip(self) -> Trip:
        return Trip.objects.create(
            name="Export me",
            creator=self.profile,
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2026, 10, 3),
        )

    def test_export_creates_event_and_link(self):
        gateway = self._patch_gateway()
        gateway.create_event.return_value = {"id": "new-evt"}
        trip = self._trip()

        link, activity_count = export_trip_to_calendar(self.account, trip, trip_url="https://example.com/t/")

        self.assertEqual(activity_count, 0)
        gateway.create_event.assert_called_once()
        body = gateway.create_event.call_args[0][0]
        self.assertEqual(body["summary"], "Export me")
        self.assertEqual(body["start"]["date"], "2026-10-01")
        self.assertEqual(body["end"]["date"], "2026-10-04")
        self.assertEqual(link.google_event_id, "new-evt")
        self.assertEqual(link.direction, CalendarSyncDirection.EXPORTED)

    def test_export_updates_existing_event(self):
        gateway = self._patch_gateway()
        gateway.update_event.return_value = {"id": "evt-old"}
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-old",
            direction=CalendarSyncDirection.EXPORTED,
        )

        export_trip_to_calendar(self.account, trip)

        gateway.update_event.assert_called_once()
        gateway.create_event.assert_not_called()
        self.assertEqual(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).count(), 1)

    def test_export_recreates_vanished_event(self):
        gateway = self._patch_gateway()
        gateway.update_event.side_effect = CalendarEventNotFoundError("gone")
        gateway.create_event.return_value = {"id": "evt-new"}
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-gone",
            direction=CalendarSyncDirection.EXPORTED,
        )

        link, _count = export_trip_to_calendar(self.account, trip)

        gateway.create_event.assert_called_once()
        self.assertEqual(link.google_event_id, "evt-new")

    def test_remove_deletes_event_and_link(self):
        gateway = self._patch_gateway()
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-x",
            direction=CalendarSyncDirection.EXPORTED,
        )

        removed = remove_trip_from_calendar(self.account, trip)

        self.assertTrue(removed)
        gateway.delete_event.assert_called_once_with("evt-x")
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_remove_without_link_is_noop(self):
        gateway = self._patch_gateway()
        trip = self._trip()

        removed = remove_trip_from_calendar(self.account, trip)

        self.assertFalse(removed)
        gateway.delete_event.assert_not_called()


class DisconnectMemberCalendarSyncTests(_CalendarSyncDBTestCase):
    """disconnect_member_calendar_sync stops a departing member's auto-sync.

    Regression coverage for a real gap: removing/leaving a trip only ever
    deleted the TripMembership row, so a departed member's Google Calendar
    kept receiving live pushes of the trip's evolving details forever via
    push_auto_synced_trip_changes - trip access control and live calendar
    export are two independent channels to the same data.
    """

    def _trip_with_link(self, *, auto_sync: bool = True) -> tuple[Trip, TripCalendarLink]:
        trip = Trip.objects.create(name="Shared trip", creator=self.profile, start_date=datetime.date(2026, 11, 1), end_date=datetime.date(2026, 11, 2))
        link = TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-departing",
            direction=CalendarSyncDirection.EXPORTED,
            auto_sync=auto_sync,
        )
        return trip, link

    def test_deletes_the_link_and_the_remote_event(self):
        gateway = self._patch_gateway()
        trip, _link = self._trip_with_link()

        disconnect_member_calendar_sync(trip, self.profile)

        gateway.delete_event.assert_called_once_with("evt-departing")
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_revoked_token_still_drops_the_link(self):
        """A failed remote delete (e.g. revoked OAuth grant) must not leave
        auto-sync active - the DB-side link is what actually re-enables
        future pushes, so it has to go regardless of the API call's outcome."""
        gateway = self._patch_gateway()
        gateway.delete_event.side_effect = GatewayRequestError("token revoked")
        trip, _link = self._trip_with_link()

        disconnect_member_calendar_sync(trip, self.profile)

        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_no_calendar_account_is_a_noop(self):
        trip, _link = self._trip_with_link()
        self.account.delete()

        disconnect_member_calendar_sync(trip, self.profile)  # must not raise

        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_no_link_is_a_noop(self):
        self._patch_gateway()
        trip = Trip.objects.create(name="Never synced", creator=self.profile)

        disconnect_member_calendar_sync(trip, self.profile)  # must not raise

    def test_departed_member_no_longer_receives_auto_sync_pushes(self):
        """End-to-end: after disconnecting, push_auto_synced_trip_changes has
        nothing left to push to for this profile."""
        gateway = self._patch_gateway()
        trip, _link = self._trip_with_link(auto_sync=True)

        disconnect_member_calendar_sync(trip, self.profile)
        synced_count = push_auto_synced_trip_changes(trip)

        self.assertEqual(synced_count, 0)
        gateway.update_event.assert_not_called()
        gateway.create_event.assert_not_called()


class TripMemberRemovalCalendarSyncTests(_CalendarSyncDBTestCase):
    """The trip member-removal/leave controllers must disconnect calendar sync."""

    def setUp(self):
        super().setUp()
        self.creator_user = User.objects.create_user(username="trip-creator")
        self.creator = self.creator_user.profile
        self.trip = Trip.objects.create(name="Group trip", creator=self.creator, start_date=datetime.date(2026, 12, 10), end_date=datetime.date(2026, 12, 12))
        TripMembership.objects.create(trip=self.trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        self.link = TripCalendarLink.objects.create(
            trip=self.trip,
            profile=self.profile,
            google_event_id="evt-member",
            direction=CalendarSyncDirection.EXPORTED,
            auto_sync=True,
        )

    def test_creator_removing_member_drops_their_calendar_link(self):
        self._patch_gateway()
        self.creator_user.set_password("pw")
        self.creator_user.save()
        self.client.force_login(self.creator_user)

        response = self.client.delete(reverse("trips.member.remove", kwargs={"trip_slug": self.trip.slug, "profile_id": self.profile.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripCalendarLink.objects.filter(pk=self.link.pk).exists())

    def test_member_leaving_drops_their_own_calendar_link(self):
        self._patch_gateway()
        self.user.set_password("pw")
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.delete(reverse("trips.leave", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripCalendarLink.objects.filter(pk=self.link.pk).exists())

    def test_removal_does_not_touch_other_members_links(self):
        self._patch_gateway()
        other_user = User.objects.create_user(username="unrelated-member")
        other_profile = other_user.profile
        TripMembership.objects.create(trip=self.trip, profile=other_profile, status=TripMembership.STATUS_JOINED)
        other_link = TripCalendarLink.objects.create(
            trip=self.trip, profile=other_profile, google_event_id="evt-other", direction=CalendarSyncDirection.EXPORTED, auto_sync=True,
        )
        self.creator_user.set_password("pw")
        self.creator_user.save()
        self.client.force_login(self.creator_user)

        self.client.delete(reverse("trips.member.remove", kwargs={"trip_slug": self.trip.slug, "profile_id": self.profile.pk}))

        self.assertTrue(TripCalendarLink.objects.filter(pk=other_link.pk).exists())


class TripCalendarExportViewTests(_CalendarSyncDBTestCase):
    """POST /trips/<uuid>/calendar/export/ can turn on auto-sync at export time."""

    def setUp(self):
        super().setUp()
        self.user.set_password("pw")
        self.user.save()
        self.client.force_login(self.user)
        self.trip = Trip.objects.create(
            name="Export via view", creator=self.profile, start_date=datetime.date(2026, 12, 1), end_date=datetime.date(2026, 12, 2),
        )

    def test_export_with_auto_sync_checked_sets_flag(self):
        gateway = self._patch_gateway()
        gateway.create_event.return_value = {"id": "view-evt"}

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}), {"auto_sync": "1"})

        self.assertEqual(response.status_code, 200)
        link = TripCalendarLink.objects.get(trip=self.trip, profile=self.profile, activity__isnull=True)
        self.assertTrue(link.auto_sync)

    def test_export_without_auto_sync_leaves_flag_off(self):
        gateway = self._patch_gateway()
        gateway.create_event.return_value = {"id": "view-evt2"}

        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}), {})

        self.assertEqual(response.status_code, 200)
        link = TripCalendarLink.objects.get(trip=self.trip, profile=self.profile, activity__isnull=True)
        self.assertFalse(link.auto_sync)


class TripCalendarAutoSyncViewTests(_CalendarSyncDBTestCase):
    """POST /trips/<slug>/calendar/auto-sync/ flips auto_sync on an existing export link."""

    def setUp(self):
        super().setUp()
        self.user.set_password("pw")
        self.user.save()
        self.client.force_login(self.user)
        self.trip = Trip.objects.create(name="Toggle me", creator=self.profile)

    def test_requires_existing_export_link(self):
        response = self.client.post(reverse("trips.calendar.autosync", kwargs={"trip_slug": self.trip.slug}), {"auto_sync": "1"})
        self.assertEqual(response.status_code, 400)

    def test_turns_auto_sync_on(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-toggle", direction=CalendarSyncDirection.EXPORTED, auto_sync=False,
        )

        response = self.client.post(reverse("trips.calendar.autosync", kwargs={"trip_slug": self.trip.slug}), {"auto_sync": "1"})

        self.assertEqual(response.status_code, 200)
        link = TripCalendarLink.objects.get(trip=self.trip, profile=self.profile, activity__isnull=True)
        self.assertTrue(link.auto_sync)

    def test_turns_auto_sync_off(self):
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-toggle2", direction=CalendarSyncDirection.EXPORTED, auto_sync=True,
        )

        response = self.client.post(reverse("trips.calendar.autosync", kwargs={"trip_slug": self.trip.slug}), {})

        self.assertEqual(response.status_code, 200)
        link = TripCalendarLink.objects.get(trip=self.trip, profile=self.profile, activity__isnull=True)
        self.assertFalse(link.auto_sync)

    def test_toggle_does_not_call_the_calendar_api(self):
        """Flipping the flag is a pure DB update - it must not spend an API call."""
        gateway = self._patch_gateway()
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-toggle3", direction=CalendarSyncDirection.EXPORTED, auto_sync=False,
        )

        self.client.post(reverse("trips.calendar.autosync", kwargs={"trip_slug": self.trip.slug}), {"auto_sync": "1"})

        gateway.update_event.assert_not_called()
        gateway.create_event.assert_not_called()


class ActivityEventBodyTests(TestCase):
    """activity_to_event_body maps scheduled activities to timed event payloads."""

    def _activity(self, **kwargs) -> TripActivity:
        trip = Trip(name="Mill weekend")
        return TripActivity(trip=trip, title="Old mill", **kwargs)

    def test_unscheduled_activity_yields_none(self):
        self.assertIsNone(activity_to_event_body(self._activity(scheduled_at=None)))

    @given(minutes=st.integers(min_value=1, max_value=60 * 24))
    def test_scheduled_end_used_when_after_start(self, minutes):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(minutes=minutes)
        body = activity_to_event_body(self._activity(scheduled_at=start, scheduled_end=end))
        self.assertEqual(body["start"]["dateTime"], start.isoformat())
        self.assertEqual(body["end"]["dateTime"], end.isoformat())

    def test_missing_end_uses_default_duration(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start))
        self.assertEqual(body["end"]["dateTime"], (start + DEFAULT_ACTIVITY_EVENT_DURATION).isoformat())

    def test_end_before_start_uses_default_duration(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start, scheduled_end=start - datetime.timedelta(hours=1)))
        self.assertEqual(body["end"]["dateTime"], (start + DEFAULT_ACTIVITY_EVENT_DURATION).isoformat())

    def test_summary_includes_trip_and_activity_names(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start))
        self.assertEqual(body["summary"], "Mill weekend: Old mill")

    def test_marks_event_as_urbanlens_export(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start))
        self.assertTrue(event_originated_from_urbanlens(body))
        self.assertIn(ACTIVITY_ID_EVENT_PROPERTY, body["extendedProperties"]["private"])

    def test_hidden_location_not_exported(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start, location_hidden=True, lat_override=41.5, lng_override=-73.9))
        self.assertNotIn("location", body)

    def test_coordinate_override_exported_as_location(self):
        start = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC)
        body = activity_to_event_body(self._activity(scheduled_at=start, lat_override=41.5, lng_override=-73.9))
        self.assertEqual(body["location"], "41.500000, -73.900000")


class ExportActivityEventsTests(_CalendarSyncDBTestCase):
    """export_trip_to_calendar mirrors scheduled activities as timed events."""

    def _trip_with_activities(self) -> tuple[Trip, TripActivity, TripActivity]:
        trip = Trip.objects.create(
            name="Export me",
            creator=self.profile,
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2026, 10, 3),
        )
        scheduled = TripActivity.objects.create(
            trip=trip,
            title="Morning mill",
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )
        unscheduled = TripActivity.objects.create(trip=trip, title="Maybe later")
        return trip, scheduled, unscheduled

    def test_export_creates_one_event_per_scheduled_activity(self):
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = [{"id": "trip-evt"}, {"id": "act-evt"}]
        trip, scheduled, _unscheduled = self._trip_with_activities()

        link, activity_count = export_trip_to_calendar(self.account, trip)

        self.assertEqual(activity_count, 1)
        self.assertEqual(link.google_event_id, "trip-evt")
        activity_link = TripCalendarLink.objects.get(trip=trip, profile=self.profile, activity=scheduled)
        self.assertEqual(activity_link.google_event_id, "act-evt")
        # Trip-level link + one activity link.
        self.assertEqual(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).count(), 2)

    def test_re_export_updates_activity_event_in_place(self):
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = [{"id": "trip-evt"}, {"id": "act-evt"}]
        gateway.update_event.side_effect = lambda event_id, _body: {"id": event_id}
        trip, _scheduled, _unscheduled = self._trip_with_activities()

        export_trip_to_calendar(self.account, trip)
        export_trip_to_calendar(self.account, trip)

        self.assertEqual(gateway.create_event.call_count, 2)
        self.assertEqual(gateway.update_event.call_count, 2)
        self.assertEqual(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).count(), 2)

    def test_unscheduling_activity_removes_its_event_on_next_export(self):
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = [{"id": "trip-evt"}, {"id": "act-evt"}]
        gateway.update_event.side_effect = lambda event_id, _body: {"id": event_id}
        trip, scheduled, _unscheduled = self._trip_with_activities()
        export_trip_to_calendar(self.account, trip)

        scheduled.scheduled_at = None
        scheduled.save(update_fields=["scheduled_at", "updated"])
        _link, activity_count = export_trip_to_calendar(self.account, trip)

        self.assertEqual(activity_count, 0)
        gateway.delete_event.assert_called_once_with("act-evt")
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile, activity__isnull=False).exists())

    def test_remove_deletes_activity_events_too(self):
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = [{"id": "trip-evt"}, {"id": "act-evt"}]
        trip, _scheduled, _unscheduled = self._trip_with_activities()
        export_trip_to_calendar(self.account, trip)

        removed = remove_trip_from_calendar(self.account, trip)

        self.assertTrue(removed)
        deleted_ids = {call.args[0] for call in gateway.delete_event.call_args_list}
        self.assertEqual(deleted_ids, {"trip-evt", "act-evt"})
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())


class PushAutoSyncedTripChangesTests(_CalendarSyncDBTestCase):
    """push_auto_synced_trip_changes mirrors a trip to every auto-synced calendar."""

    def _trip(self) -> Trip:
        return Trip.objects.create(
            name="Auto-synced",
            creator=self.profile,
            start_date=datetime.date(2026, 11, 1),
            end_date=datetime.date(2026, 11, 2),
        )

    def test_pushes_trip_with_auto_sync_link(self):
        gateway = self._patch_gateway()
        gateway.update_event.side_effect = lambda event_id, _body: {"id": event_id}
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip, profile=self.profile, google_event_id="evt-auto", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )

        synced = push_auto_synced_trip_changes(trip)

        self.assertEqual(synced, 1)
        gateway.update_event.assert_called_once()
        self.assertEqual(gateway.update_event.call_args[0][0], "evt-auto")

    def test_skips_link_without_auto_sync(self):
        gateway = self._patch_gateway()
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip, profile=self.profile, google_event_id="evt-manual", direction=CalendarSyncDirection.EXPORTED, auto_sync=False,
        )

        synced = push_auto_synced_trip_changes(trip)

        self.assertEqual(synced, 0)
        gateway.update_event.assert_not_called()
        gateway.create_event.assert_not_called()

    def test_no_links_is_a_noop(self):
        gateway = self._patch_gateway()
        trip = self._trip()

        synced = push_auto_synced_trip_changes(trip)

        self.assertEqual(synced, 0)
        gateway.update_event.assert_not_called()

    def test_gateway_failure_on_one_profile_does_not_block_others(self):
        other_user = User.objects.create_user(username="other-calendar-tester")
        other_profile, _ = Profile.objects.get_or_create(user=other_user)
        other_account = GoogleCalendarAccount.objects.create(
            profile=other_profile,
            access_token="access2",  # noqa: S106
            refresh_token="refresh2",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        trip = self._trip()
        TripCalendarLink.objects.create(
            trip=trip, profile=self.profile, google_event_id="evt-fails", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )
        TripCalendarLink.objects.create(
            trip=trip, profile=other_profile, google_event_id="evt-ok", direction=CalendarSyncDirection.IMPORTED, auto_sync=True,
        )

        gateway_cls = mock.patch("urbanlens.dashboard.services.calendar_sync.GoogleCalendarGateway").start()
        self.addCleanup(mock.patch.stopall)

        def _gateway_for(*, account):
            instance = mock.Mock()
            if account.pk == self.account.pk:
                instance.update_event.side_effect = GatewayRequestError("token revoked")
            else:
                instance.update_event.side_effect = lambda event_id, _body: {"id": event_id}
            return instance

        gateway_cls.side_effect = _gateway_for

        synced = push_auto_synced_trip_changes(trip)

        self.assertEqual(synced, 1)
        self.assertIsNotNone(other_account)


class GetCalendarAccountTests(_CalendarSyncDBTestCase):
    """GoogleCalendarAccountManager.get_for_profile() heals accounts left with undecryptable tokens.

    Regression test for a production 500: rotating field_encryption_key
    without migrating old rows makes EncryptedTextField.from_db_value raise
    InvalidToken, which crashed every page that touched the calendar
    connection (e.g. GET /dashboard/trips/).
    """

    def _corrupt_stored_access_token(self):
        """Write a ciphertext-shaped value directly to the DB that Fernet cannot decrypt."""
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_google_calendar_accounts SET access_token = %s WHERE id = %s",
                ["not-a-valid-fernet-token", self.account.id],
            )

    def test_returns_account_when_decryptable(self):
        self.assertEqual(GoogleCalendarAccount.objects.get_for_profile(self.profile), self.account)

    def test_undecryptable_account_is_healed_to_none(self):
        self._corrupt_stored_access_token()
        self.assertIsNone(GoogleCalendarAccount.objects.get_for_profile(self.profile))
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())

    def test_raw_query_still_raises_invalid_token(self):
        """Sanity check that the corruption helper actually reproduces the bug."""
        self._corrupt_stored_access_token()
        with pytest.raises(InvalidToken):
            GoogleCalendarAccount.objects.filter(profile=self.profile).first()

    def test_trips_list_page_does_not_500_with_undecryptable_account(self):
        self._corrupt_stored_access_token()
        self.client.force_login(self.user)
        response = self.client.get(reverse("trips.list"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())


class CalendarCallbackViewTests(TestCase):
    """The OAuth callback rejects tampered or missing state without saving tokens."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="callback-tester")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)

    def test_bad_state_redirects_without_creating_account(self):
        response = self.client.get(reverse("trips.calendar.callback"), {"state": "forged", "code": "abc"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())

    def test_provider_error_redirects_without_creating_account(self):
        response = self.client.get(reverse("trips.calendar.callback"), {"error": "access_denied"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GoogleCalendarAccount.objects.filter(profile=self.profile).exists())
