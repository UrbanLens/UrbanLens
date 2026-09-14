"""`CalendarImportView`: the calendar import dialog and the import action (P57).

The import service is tested in `test_calendar_sync.py`; these cover what only the view does - the no-account
responses, turning the POST's per-event fields into selections, the expired-grant and gateway-failure branches, and the
toast it reports.
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError

_LIST = "urbanlens.dashboard.controllers.calendar_sync.list_importable_events"
_IMPORT = "urbanlens.dashboard.controllers.calendar_sync.import_events_as_trips"
_GATEWAY = "urbanlens.dashboard.services.trips.calendar_sync.GoogleCalendarGateway"
_UPSTREAM_TEXT = "upstream said: invalid_grant for refresh token 1//0g"


class _ImportViewTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="calendar-importer")
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.account = GoogleCalendarAccount.objects.create(
            profile=self.profile,
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            token_expiry=timezone.now() + datetime.timedelta(hours=1),
        )
        self.client.force_login(self.user)
        self.url = reverse("trips.calendar.import")

    def _toast(self, response) -> dict:
        return json.loads(response["HX-Trigger"])["showToast"]


class CalendarImportDialogTests(_ImportViewTestCase):
    def test_without_a_connected_calendar_it_asks_to_connect(self) -> None:
        self.account.delete()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect your Google Calendar first.")

    def test_upcoming_events_are_listed_for_selection(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.list_events.return_value = [
                {
                    "id": "evt-mill",
                    "summary": "Mill walk",
                    "start": {"date": "2026-09-04"},
                    "end": {"date": "2026-09-05"},
                },
            ]
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mill walk")
        self.assertContains(response, 'value="evt-mill"')

    def test_an_expired_grant_drops_the_connection_and_asks_to_reconnect(self) -> None:
        with mock.patch(_LIST, side_effect=GoogleAuthExpiredError("expired")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "connection has expired")
        self.assertFalse(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())

    def test_a_gateway_failure_keeps_the_connection_and_hides_the_upstream_text(self) -> None:
        with mock.patch(_LIST, side_effect=GatewayRequestError(_UPSTREAM_TEXT)):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be reached")
        self.assertNotContains(response, "invalid_grant")
        self.assertTrue(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())


class CalendarImportActionTests(_ImportViewTestCase):
    def test_without_a_connected_calendar_it_is_refused(self) -> None:
        self.account.delete()

        response = self.client.post(self.url, {"event_ids": ["evt-1"]})

        self.assertEqual(response.status_code, 400)

    def test_blank_event_ids_are_not_a_selection(self) -> None:
        with mock.patch(_IMPORT) as import_events:
            response = self.client.post(self.url, {"event_ids": ["", "  "]})

        self.assertEqual(response.status_code, 400)
        import_events.assert_not_called()

    def test_per_event_fields_become_selections(self) -> None:
        with mock.patch(_IMPORT, return_value=([], [], 0)) as import_events:
            self.client.post(
                self.url,
                {
                    "event_ids": ["evt-1", " ", "evt-2"],
                    "create_activity_evt-1": "1",
                    "invite_evt-1": ["5", "x", "-3", "7"],
                    "auto_sync_evt-2": "1",
                    "create_activity_evt-2": "0",
                },
            )

        import_events.assert_called_once()
        self.assertEqual(
            import_events.call_args.args[1],
            [
                {"event_id": "evt-1", "create_activity": True, "invite_profile_ids": [5, 7], "auto_sync": False},
                {"event_id": "evt-2", "create_activity": False, "invite_profile_ids": [], "auto_sync": True},
            ],
        )

    def test_an_event_is_imported_as_a_trip_and_reported(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.get_event.return_value = {
                "id": "evt-foundry",
                "summary": "Foundry day",
                "start": {"date": "2026-09-04"},
                "end": {"date": "2026-09-05"},
            }
            response = self.client.post(self.url, {"event_ids": ["evt-foundry"]})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Trip.objects.filter(name="Foundry day", creator=self.profile).exists())
        self.assertEqual(self._toast(response), {"level": "success", "message": "Imported 1 event as trips."})

    def test_invitations_are_added_to_the_report(self) -> None:
        with mock.patch(_IMPORT, return_value=([mock.sentinel.trip, mock.sentinel.other], [], 2)):
            response = self.client.post(self.url, {"event_ids": ["evt-1", "evt-2"]})

        self.assertEqual(
            self._toast(response),
            {"level": "success", "message": "Imported 2 events as trips. Invited 2 participants."},
        )

    def test_nothing_imported_is_a_warning_carrying_the_one_skip_reason(self) -> None:
        reason = "An event was skipped because it is already linked to a trip."
        with mock.patch(_IMPORT, return_value=([], [reason], 0)):
            response = self.client.post(self.url, {"event_ids": ["evt-1"]})

        self.assertEqual(self._toast(response), {"level": "warning", "message": f"No events were imported. {reason}"})

    def test_several_skips_are_counted_rather_than_listed(self) -> None:
        with mock.patch(_IMPORT, return_value=([], ["first reason", "second reason"], 0)):
            response = self.client.post(self.url, {"event_ids": ["evt-1", "evt-2"]})

        self.assertEqual(
            self._toast(response), {"level": "warning", "message": "No events were imported. 2 items were skipped."}
        )

    def test_an_expired_grant_drops_the_connection(self) -> None:
        with mock.patch(_IMPORT, side_effect=GoogleAuthExpiredError("expired")):
            response = self.client.post(self.url, {"event_ids": ["evt-1"]})

        self.assertEqual(response.status_code, 502)
        self.assertIn("connection has expired", response.content.decode())
        self.assertFalse(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())

    def test_a_gateway_failure_hides_the_upstream_text(self) -> None:
        with mock.patch(_IMPORT, side_effect=GatewayRequestError(_UPSTREAM_TEXT)):
            response = self.client.post(self.url, {"event_ids": ["evt-1"]})

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("invalid_grant", response.content.decode())
        self.assertTrue(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())
