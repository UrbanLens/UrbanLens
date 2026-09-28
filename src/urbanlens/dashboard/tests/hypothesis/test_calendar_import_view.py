"""`CalendarImportView`: the calendar import dialog and the import action (P57).

The import service is tested in `test_calendar_sync.py`; these cover what only the view and the background import
do - the no-account responses, turning the POST's per-event fields into a queued task's selections, the progress poll
and who may read it, the expired-grant and gateway-failure outcomes, and the toast reported.
"""

from __future__ import annotations

import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.apis.calendar.google import EventListing
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.core.celery import TaskProgress
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.trips.calendar_sync import MAX_IMPORTABLE_EVENTS, import_summary, run_calendar_import

_LIST = "urbanlens.dashboard.controllers.calendar_sync.list_importable_events"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_PROGRESS = "urbanlens.dashboard.services.core.celery.get_task_progress"
_SERVICE_IMPORT = "urbanlens.dashboard.services.trips.calendar_sync.import_events_as_trips"
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
            gateway_cls.return_value.list_events.return_value = EventListing(
                [
                    {
                        "id": "evt-mill",
                        "summary": "Mill walk",
                        "start": {"date": "2026-09-04"},
                        "end": {"date": "2026-09-05"},
                    },
                ]
            )
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
        with mock.patch(_ENQUEUE) as enqueue:
            response = self.client.post(self.url, {"event_ids": ["", "  "]})

        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()

    def test_more_events_than_the_dialog_lists_are_refused(self) -> None:
        ids = [f"evt-{index}" for index in range(MAX_IMPORTABLE_EVENTS + 1)]
        with mock.patch(_ENQUEUE) as enqueue:
            response = self.client.post(self.url, {"event_ids": ids})

        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()

    def test_per_event_fields_become_selections_on_a_queued_task(self) -> None:
        with mock.patch(_ENQUEUE, return_value=mock.Mock(id="task-1")) as enqueue:
            response = self.client.post(
                self.url,
                {
                    "event_ids": ["evt-1", " ", "evt-2", "evt-1"],
                    "create_activity_evt-1": "1",
                    "invite_evt-1": ["5", "x", "-3", "7"],
                    "auto_sync_evt-2": "1",
                    "create_activity_evt-2": "0",
                },
            )

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.profile.pk)
        self.assertEqual(
            enqueue.call_args.args[2],
            [
                {"event_id": "evt-1", "create_activity": True, "invite_profile_ids": [5, 7], "auto_sync": False},
                {"event_id": "evt-2", "create_activity": False, "invite_profile_ids": [], "auto_sync": True},
            ],
        )
        self.assertIn("/trips/calendar/import/progress/", response.content.decode())


class CalendarImportProgressTests(_ImportViewTestCase):
    def _progress_url(self, profile_pk: int) -> str:
        with mock.patch(_ENQUEUE, return_value=mock.Mock(id="task-1")):
            self.client.post(self.url, {"event_ids": ["evt-1"]})
        token = signing.dumps({"task": "task-1", "profile": profile_pk}, salt="google-calendar-import-progress")
        return reverse("trips.calendar.import.progress", kwargs={"token": token})

    def test_a_finished_import_toasts_its_outcome_and_refreshes_the_trip_list(self) -> None:
        done = TaskProgress(
            task_id="task-1",
            state="SUCCESS",
            percent=100,
            result={"level": "success", "message": "Imported 1 event as trips.", "created": 1},
        )
        with mock.patch(_PROGRESS, return_value=done):
            response = self.client.get(self._progress_url(self.profile.pk))

        self.assertEqual(self._toast(response), {"level": "success", "message": "Imported 1 event as trips."})
        self.assertContains(response, 'hx-select="#trip-list"')

    def test_another_profiles_import_is_not_readable(self) -> None:
        other = User.objects.create_user(username="someone-else").profile
        with mock.patch(_PROGRESS) as progress:
            response = self.client.get(self._progress_url(other.pk))

        self.assertEqual(response.status_code, 404)
        progress.assert_not_called()

    def test_a_forged_token_is_not_readable(self) -> None:
        response = self.client.get(reverse("trips.calendar.import.progress", kwargs={"token": "task-1"}))

        self.assertEqual(response.status_code, 404)


class RunCalendarImportTests(_ImportViewTestCase):
    def test_an_event_is_imported_as_a_trip_and_reported(self) -> None:
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.list_events.return_value = EventListing(
                [
                    {
                        "id": "evt-foundry",
                        "summary": "Foundry day",
                        "start": {"date": "2026-09-04"},
                        "end": {"date": "2026-09-05"},
                    }
                ]
            )
            outcome = run_calendar_import(self.profile.pk, [{"event_id": "evt-foundry"}])

        self.assertTrue(Trip.objects.filter(name="Foundry day", creator=self.profile).exists())
        self.assertEqual(outcome, {"level": "success", "message": "Imported 1 event as trips.", "created": 1})

    def test_the_calendar_is_read_once_however_many_events_are_named(self) -> None:
        """One paged listing, not a 30-second request per posted id (G4-6)."""
        events = [
            {
                "id": f"evt-{index}",
                "summary": f"Trip {index}",
                "start": {"date": "2026-09-04"},
                "end": {"date": "2026-09-05"},
            }
            for index in range(20)
        ]
        with mock.patch(_GATEWAY) as gateway_cls:
            gateway_cls.return_value.list_events.return_value = EventListing(events)
            outcome = run_calendar_import(self.profile.pk, [{"event_id": event["id"]} for event in events])

        self.assertEqual(outcome["created"], 20)
        gateway_cls.return_value.list_events.assert_called_once()
        gateway_cls.return_value.get_event.assert_not_called()

    def test_an_expired_grant_drops_the_connection(self) -> None:
        with mock.patch(_SERVICE_IMPORT, side_effect=GoogleAuthExpiredError("expired")):
            outcome = run_calendar_import(self.profile.pk, [{"event_id": "evt-1"}])

        self.assertEqual(outcome["level"], "error")
        self.assertIn("connection has expired", outcome["message"])
        self.assertFalse(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())

    def test_a_gateway_failure_hides_the_upstream_text(self) -> None:
        with mock.patch(_SERVICE_IMPORT, side_effect=GatewayRequestError(_UPSTREAM_TEXT)):
            outcome = run_calendar_import(self.profile.pk, [{"event_id": "evt-1"}])

        self.assertEqual(outcome["level"], "error")
        self.assertNotIn("invalid_grant", outcome["message"])
        self.assertTrue(GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists())


class ImportSummaryTests(SimpleTestCase):
    def test_invitations_are_added_to_the_report(self) -> None:
        self.assertEqual(import_summary(2, [], 2), ("success", "Imported 2 events as trips. Invited 2 participants."))

    def test_nothing_imported_is_a_warning_carrying_the_one_skip_reason(self) -> None:
        reason = "An event was skipped because it is already linked to a trip."
        self.assertEqual(import_summary(0, [reason], 0), ("warning", f"No events were imported. {reason}"))

    def test_several_skips_are_counted_rather_than_listed(self) -> None:
        self.assertEqual(
            import_summary(0, ["first reason", "second reason"], 0),
            ("warning", "No events were imported. 2 items were skipped."),
        )
