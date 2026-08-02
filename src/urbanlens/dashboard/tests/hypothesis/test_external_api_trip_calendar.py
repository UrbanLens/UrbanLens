"""Tests for exporting a trip to Google Calendar over the external API.

Two things are covered here, and the first is not an API concern at all:

**The export privacy gate.** ``services.trips.calendar_sync`` used to honour only an
activity's own ``location_hidden`` flag when writing event locations, ignoring
the adder's ``trip_pin_location_visibility`` setting that every other trip
surface applies (the activities panel, the map, AI suggestions - all via
``services.trips.trip_visibility.viewer_hidden_activity_ids``). Exporting a shared
trip therefore copied trip-mates' coordinates - ones the trip screen
deliberately hides from the exporter - into a third party's calendar, where no
UrbanLens setting can ever claw them back. The first class below is the
regression guard, written against the service rather than the endpoint because
auto-sync pushes reach the same code without any HTTP request at all.

**The endpoint pair.** ``POST``/``DELETE /trips/{slug}/calendar/`` exists
because export was assumed to be impossible without a browser: it is not.
Tokens are stored per user *with a refresh token* and the gateway refreshes
them on its own, so only the one-time consent is browser-bound. The 409 for an
unconnected calendar therefore carries the *site's own* connect route rather
than a Google URL - a Google URL minted here would redirect to the login page
and lose the authorization code, because the API caller has no session.

Every gateway call is mocked. Nothing in this file may reach Google.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.apis.calendar.google import CalendarNotConfiguredError
from urbanlens.dashboard.services.trips.calendar_sync import export_trip_to_calendar
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError

_TRIP_SCOPES = [ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]

#: ``Location.address`` is a read-only property assembled from components, so
#: the fixture sets the components and asserts against the string they build.
_ADDRESS_COMPONENTS = {
    "street_number": "1580",
    "route": "E Grand Blvd",
    "locality": "Detroit",
    "administrative_area_level_1": "MI",
}
_ADDRESS = "1580 E Grand Blvd, Detroit, MI"


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token.

    Args:
        raw_key: The raw (unhashed) API key value.

    Returns:
        Extra kwargs for Django's test client.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _CalendarTestCase(TestCase):
    """Shared fixture: two profiles, a connected calendar, and a mocked gateway."""

    def setUp(self) -> None:
        """Create the exporter, a trip-mate, an API key, and a calendar account."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="exporter")
        self.profile = Profile.objects.get(user=self.user)
        self.mate_user = baker.make(User, username="tripmate")
        self.mate = Profile.objects.get(user=self.mate_user)

        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[*_TRIP_SCOPES, ApiKeyScope.PROFILE_READ.value])

        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            google_email="exporter@example.com",
            access_token="access",  # noqa: S106 - fixture value, not a real credential
            refresh_token="refresh",  # noqa: S106 - fixture value, not a real credential
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )

    def _patch_gateway(self) -> mock.MagicMock:
        """Replace the gateway the sync service instantiates.

        Returns:
            The mock standing in for the gateway *instance*, with
            ``create_event`` already returning a plausible event id.
        """
        patcher = mock.patch("urbanlens.dashboard.services.trips.calendar_sync.GoogleCalendarGateway")
        gateway_cls = patcher.start()
        self.addCleanup(patcher.stop)
        gateway = gateway_cls.return_value
        gateway.create_event.return_value = {"id": "evt-new"}
        gateway.update_event.return_value = {"id": "evt-new"}
        return gateway

    def _shared_trip(self) -> Trip:
        """A dated trip the key owner and the trip-mate both belong to.

        Returns:
            The saved trip, created by the trip-mate so the key owner is a
            plain member rather than an organizer.
        """
        trip = Trip.objects.create(
            name="Shared trip",
            creator=self.mate,
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2026, 10, 3),
        )
        TripMembership.objects.create(trip=trip, profile=self.mate, status=TripMembership.STATUS_JOINED, rsvp="yes")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED, rsvp="yes")
        return trip

    def _own_trip(self) -> Trip:
        """A dated trip the key owner created and joined.

        Returns:
            The saved trip.
        """
        trip = Trip.objects.create(
            name="Own trip",
            creator=self.profile,
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2026, 10, 3),
        )
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED, rsvp="yes")
        return trip


class ExportRespectsAdderVisibilityTests(_CalendarTestCase):
    """A trip-mate's hidden coordinates must never reach a third party's calendar.

    ``location_hidden`` was the only gate the exporter honoured. The adder's
    ``trip_pin_location_visibility`` - the setting that decides whether *this
    particular viewer* sees a location at all - was ignored, so the exporter
    received in their Google Calendar exactly the coordinates the trip screen
    had refused to show them.
    """

    def _mate_activity(self, trip: Trip, visibility: str, **kwargs) -> TripActivity:
        """Add a located activity contributed by the trip-mate.

        Args:
            trip: The trip to add to.
            visibility: The adder's ``trip_pin_location_visibility`` level.
            **kwargs: Extra ``TripActivity`` field values.

        Returns:
            The saved activity, with ``added_by`` refreshed so the visibility
            gate reads the level just set.
        """
        Profile.objects.filter(pk=self.mate.pk).update(trip_pin_location_visibility=visibility)
        location = Location.objects.create(latitude=42.33, longitude=-83.04, **_ADDRESS_COMPONENTS)
        return TripActivity.objects.create(trip=trip, location=location, added_by=self.mate, title="Packard Plant", **kwargs)

    def test_trip_event_omits_a_location_the_exporter_may_not_see(self) -> None:
        """The all-day trip event must not carry a hidden trip-mate's address."""
        gateway = self._patch_gateway()
        trip = self._shared_trip()
        self._mate_activity(trip, VisibilityChoice.NO_ONE)

        export_trip_to_calendar(self.account, trip)

        body = gateway.create_event.call_args_list[0][0][0]
        self.assertNotIn("location", body)

    def test_activity_event_omits_a_location_the_exporter_may_not_see(self) -> None:
        """The per-activity timed event must not carry it either."""
        gateway = self._patch_gateway()
        trip = self._shared_trip()
        self._mate_activity(
            trip,
            VisibilityChoice.NO_ONE,
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )

        export_trip_to_calendar(self.account, trip)

        bodies = [call[0][0] for call in gateway.create_event.call_args_list]
        activity_bodies = [body for body in bodies if "dateTime" in body["start"]]
        self.assertEqual(len(activity_bodies), 1)
        self.assertNotIn("location", activity_bodies[0])

    def test_a_visible_location_is_still_exported(self) -> None:
        """The gate must not swallow locations the exporter is allowed to see."""
        gateway = self._patch_gateway()
        trip = self._shared_trip()
        self._mate_activity(trip, VisibilityChoice.ANYONE)

        export_trip_to_calendar(self.account, trip)

        body = gateway.create_event.call_args_list[0][0][0]
        self.assertEqual(body["location"], _ADDRESS)

    def test_the_adders_own_export_still_carries_their_location(self) -> None:
        """A restrictive setting hides a location from others, never from its owner."""
        gateway = self._patch_gateway()
        trip = self._shared_trip()
        self._mate_activity(trip, VisibilityChoice.NO_ONE)
        mate_account = GoogleCalendarAccount.objects.create(
            profile=self.mate,
            access_token="access",  # noqa: S106 - fixture value, not a real credential
            refresh_token="refresh",  # noqa: S106 - fixture value, not a real credential
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )

        export_trip_to_calendar(mate_account, trip)

        body = gateway.create_event.call_args_list[0][0][0]
        self.assertEqual(body["location"], _ADDRESS)


class TripCalendarExportEndpointTests(_CalendarTestCase):
    """``POST /trips/{slug}/calendar/`` mirrors a trip onto the caller's calendar."""

    def _post(self, trip: Trip, body: dict | None = None, raw_key: str | None = None):
        """POST the export endpoint for *trip*.

        Args:
            trip: The trip to export.
            body: Optional JSON body.
            raw_key: Optional alternative API key.

        Returns:
            The test client's response.
        """
        return self.client.post(
            reverse("external_api:trips.calendar_export", args=[trip.slug]),
            body or {},
            content_type="application/json",
            **_bearer(raw_key or self.raw_key),
        )

    def test_export_creates_the_event_and_reports_status(self) -> None:
        """A successful export answers with the refreshed status block."""
        gateway = self._patch_gateway()
        trip = self._own_trip()
        TripActivity.objects.create(
            trip=trip,
            added_by=self.profile,
            title="Scheduled stop",
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )

        response = self._post(trip)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["activities_exported"], 1)
        self.assertTrue(body["calendar"]["connected"])
        self.assertTrue(body["calendar"]["linked"])
        self.assertEqual(body["calendar"]["account_email"], "exporter@example.com")
        self.assertEqual(gateway.create_event.call_count, 2)

    def test_auto_sync_flag_is_applied(self) -> None:
        """``auto_sync`` is persisted on the link the export just created."""
        self._patch_gateway()
        trip = self._own_trip()

        response = self._post(trip, {"auto_sync": True})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["calendar"]["auto_sync"])
        self.assertTrue(TripCalendarLink.objects.trip_level_link(trip, self.profile).auto_sync)

    def test_export_opens_no_transaction_around_the_upstream_calls(self) -> None:
        """The third-party calls must not run inside a transaction this view opened.

        Wrapping the fan-out in ``transaction.atomic`` would pin a database
        connection for the length of an unbounded series of upstream requests.
        The service is idempotent by design precisely so it does not need one.

        The assertion is on *savepoint depth* rather than ``in_atomic_block``:
        Django's ``TestCase`` already runs every test inside an atomic block, so
        the latter is unconditionally true here and would pass no matter what
        the view did. Any ``atomic`` the request opened would nest inside that
        outer block and push a savepoint, which is what this actually detects.
        """
        from django.db import connection

        gateway = self._patch_gateway()
        trip = self._own_trip()
        TripActivity.objects.create(
            trip=trip,
            added_by=self.profile,
            title="Scheduled stop",
            scheduled_at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC),
        )
        baseline = len(connection.savepoint_ids)
        depths: list[int] = []

        def _record(_body):
            """Record the savepoint depth in force when the gateway is called."""
            depths.append(len(connection.savepoint_ids))
            return {"id": "evt-new"}

        gateway.create_event.side_effect = _record

        self._post(trip)

        self.assertEqual(len(depths), 2)
        self.assertTrue(all(depth == baseline for depth in depths))

    def test_undated_trip_is_400(self) -> None:
        """A trip with nothing to place on a calendar is a client error."""
        self._patch_gateway()
        trip = Trip.objects.create(name="Dateless", creator=self.profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)

        response = self._post(trip)

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_unconnected_calendar_is_409_with_the_sites_connect_url(self) -> None:
        """The client must be sent to the site's own route, never to Google.

        A Google authorization URL minted by this API would come back to the
        OAuth callback with no session attached, redirect to the login page,
        and lose the authorization code.
        """
        self._patch_gateway()
        self.account.delete()
        trip = self._own_trip()

        response = self._post(trip)

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error_code"], "calendar_not_connected")
        # Absolute, because the client opens it in a system browser rather than
        # resolving it against its own API base.
        self.assertTrue(body["authorization_url"].startswith("http"))
        self.assertTrue(body["authorization_url"].endswith(f"{reverse('trips.calendar.connect')}?next=trips.list"))

    def test_dead_grant_is_409_and_drops_the_account(self) -> None:
        """A rejected grant is discarded so the next attempt is not the same failure."""
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = GoogleAuthExpiredError("revoked")
        trip = self._own_trip()

        response = self._post(trip)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "calendar_reauthorization_required")
        self.assertFalse(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())

    def test_gateway_failure_is_502(self) -> None:
        """A transient upstream failure is reported as a bad gateway, not a 500."""
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = GatewayRequestError("calendar unavailable")
        trip = self._own_trip()

        response = self._post(trip)

        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())

    def test_unconfigured_deployment_is_503(self) -> None:
        """No OAuth client on this deployment is a server-side unavailability."""
        gateway = self._patch_gateway()
        gateway.create_event.side_effect = CalendarNotConfiguredError("no client id")
        trip = self._own_trip()

        response = self._post(trip)

        self.assertEqual(response.status_code, 503)

    def test_non_member_trip_is_404_not_403(self) -> None:
        """A trip the caller is not on must look exactly like one that never existed."""
        self._patch_gateway()
        theirs = Trip.objects.create(name="Private", creator=self.mate, start_date=datetime.date(2026, 10, 1))
        TripMembership.objects.create(trip=theirs, profile=self.mate, status=TripMembership.STATUS_JOINED)

        forbidden = self._post(theirs)
        missing = self.client.post(
            reverse("external_api:trips.calendar_export", args=["no-such-trip-slug"]),
            {},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(forbidden.json(), missing.json())

    def test_read_scope_alone_cannot_export(self) -> None:
        """Export is a write, and a read-only credential must be refused."""
        self._patch_gateway()
        api_key, raw = generate_api_key(self.user, "Read only")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.TRIPS_READ.value])
        trip = self._own_trip()

        response = self._post(trip, raw_key=raw)

        self.assertEqual(response.status_code, 403)

    def test_export_carries_its_own_throttle_bucket(self) -> None:
        """The calendar tier is stacked on top of the standard three."""
        from urbanlens.dashboard.external_api.throttling import CalendarExportThrottle
        from urbanlens.dashboard.external_api.views_trips import TripCalendarExportView

        self.assertIn(CalendarExportThrottle, TripCalendarExportView.throttle_classes)


class TripCalendarRemoveEndpointTests(_CalendarTestCase):
    """``DELETE /trips/{slug}/calendar/`` takes a trip back off the calendar."""

    def _delete(self, trip: Trip):
        """DELETE the export endpoint for *trip*.

        Args:
            trip: The trip to unexport.

        Returns:
            The test client's response.
        """
        return self.client.delete(reverse("external_api:trips.calendar_export", args=[trip.slug]), **_bearer(self.raw_key))

    def test_remove_deletes_the_event_and_the_link(self) -> None:
        """A linked trip is removed upstream and locally, and reports ``removed``."""
        gateway = self._patch_gateway()
        trip = self._own_trip()
        TripCalendarLink.objects.create(
            trip=trip,
            profile=self.profile,
            google_event_id="evt-x",
            direction=CalendarSyncDirection.EXPORTED,
        )

        response = self._delete(trip)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["removed"])
        self.assertFalse(body["calendar"]["linked"])
        gateway.delete_event.assert_called_once_with("evt-x")
        self.assertFalse(TripCalendarLink.objects.filter(trip=trip, profile=self.profile).exists())

    def test_remove_without_a_link_reports_removed_false(self) -> None:
        """Unexporting something that was never exported is a no-op, not an error."""
        gateway = self._patch_gateway()
        trip = self._own_trip()

        response = self._delete(trip)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["removed"])
        gateway.delete_event.assert_not_called()

    def test_remove_without_a_calendar_is_409(self) -> None:
        """With no connected account there is nothing that could hold the event."""
        self._patch_gateway()
        self.account.delete()
        trip = self._own_trip()

        response = self._delete(trip)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "calendar_not_connected")
