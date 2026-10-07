"""A Google failure that passes, or one about the site rather than the user, keeps the calendar connection (P337).

Only Google's refusal of the user's grant drops it: ``invalid_grant`` from the token endpoint, a 401 that a fresh token
does not cure, or a 403 that is neither a rate limit, a refusal of the site's Google project, nor a refusal of one event.
A refresh that gets a 5xx, a 429 or no answer at all is busy; a refusal of the site's OAuth client or Google project is
logged at ERROR and reported as calendar sync being unavailable.

The token endpoint's answers follow RFC 6749 section 5.2 and Google's "Using OAuth 2.0 to Access Google APIs"
(refresh token expiration). The Calendar API bodies are the ones its "Handle API errors" guide prints, and the
disabled-API body is Google's ``accessNotConfigured`` / ``SERVICE_DISABLED`` envelope.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest import mock

from django.urls import reverse
from django.utils import timezone
import requests

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount, TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.apis.calendar import google as calendar_google
from urbanlens.dashboard.services.apis.calendar.google import (
    CalendarBusyError,
    CalendarUnavailableError,
    GoogleCalendarGateway,
    is_site_refusal,
)
from urbanlens.dashboard.services.auth import google_oauth
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.auth.google_oauth import (
    GoogleAuthExpiredError,
    GoogleOAuthClientRefusedError,
    GoogleTokenServiceBusyError,
)
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.trips import calendar_sync
from urbanlens.dashboard.services.trips.calendar_sync import (
    export_trip_to_calendar,
    push_auto_synced_trip_changes,
    run_calendar_import,
)
from urbanlens.dashboard.tasks import (
    MAX_CALENDAR_PUSH_ATTEMPTS,
    MAX_OWED_CALENDAR_WRITE_AGE,
    requeue_pending_calendar_pushes,
)
from urbanlens.dashboard.tests.hypothesis.test_calendar_withheld_fields import (
    INSUFFICIENT_SCOPE,
    INVALID_CREDENTIALS,
    RATE_LIMIT,
    _google_error,
    _raw_response,
    _TripWithAMateCase,
)

_PROJECT_MESSAGE = (
    "Google Calendar API has not been used in project 123456789012 before or it is disabled. Enable it by visiting "
    "https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=123456789012 then "
    "retry. If you enabled this API recently, wait a few minutes for the action to propagate to our systems and retry."
)
#: The Calendar API disabled on the site's Google project, in the envelope Google sends now: the legacy reason and
#: the ``ErrorInfo`` together, with PERMISSION_DENIED as its status.
API_DISABLED = {
    "error": {
        "code": 403,
        "message": _PROJECT_MESSAGE,
        "errors": [
            {
                "message": _PROJECT_MESSAGE,
                "domain": "usageLimits",
                "reason": "accessNotConfigured",
                "extendedHelp": "https://console.developers.google.com",
            }
        ],
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "SERVICE_DISABLED",
                "domain": "googleapis.com",
                "metadata": {"consumer": "projects/123456789012", "service": "calendar-json.googleapis.com"},
            }
        ],
    }
}
#: The same refusal as only the legacy body carries it.
API_NOT_CONFIGURED = _google_error(403, "accessNotConfigured", _PROJECT_MESSAGE)
#: The same refusal as only the newer envelope carries it.
SERVICE_DISABLED_ONLY = {
    "error": {
        "code": 403,
        "message": _PROJECT_MESSAGE,
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "SERVICE_DISABLED",
                "domain": "googleapis.com",
            }
        ],
    }
}
BILLING_DISABLED = {
    "error": {
        "code": 403,
        "message": "This API method requires billing to be enabled.",
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "BILLING_DISABLED",
                "domain": "googleapis.com",
            }
        ],
    }
}
_SITE_REFUSALS = {
    "accessNotConfigured and SERVICE_DISABLED": API_DISABLED,
    "accessNotConfigured": API_NOT_CONFIGURED,
    "SERVICE_DISABLED": SERVICE_DISABLED_ONLY,
    "BILLING_DISABLED": BILLING_DISABLED,
}
#: The Calendar guide's 403 for changing a shared property on a copy that is not the organizer's: about one event.
NON_ORGANIZER = _google_error(
    403,
    "forbiddenForNonOrganizer",
    "Shared properties can only be changed by the organizer of the event.",
    domain="calendar",
)

INVALID_GRANT = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
_TOKEN_BUSY: dict[str, tuple[int, dict[str, Any]] | Exception] = {
    "500": (500, {"error": "internal_failure", "error_description": "Backend Error"}),
    "503": (503, {"error": "temporarily_unavailable"}),
    "429": (429, {"error": "rate_limit_exceeded"}),
    "timeout": requests.Timeout("read timed out"),
    "no connection": requests.ConnectionError("connection refused"),
}
_TOKEN_CLIENT_REFUSALS: dict[str, tuple[int, dict[str, Any]]] = {
    "invalid_client": (401, {"error": "invalid_client", "error_description": "The OAuth client was not found."}),
    "unauthorized_client": (400, {"error": "unauthorized_client", "error_description": "Unauthorized"}),
}


class _ExpiredTokenCase(_TripWithAMateCase):
    """The exporter's access token has run out, so the first calendar call refreshes it at Google's token endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self._expire_token()

    def _expire_token(self) -> None:
        GoogleCalendarAccount.objects.filter(pk=self.account.pk).update(
            token_expiry=timezone.now() - datetime.timedelta(minutes=5)
        )
        self.account.refresh_from_db()

    def _fresh_token(self) -> None:
        GoogleCalendarAccount.objects.filter(pk=self.account.pk).update(
            token_expiry=timezone.now() + datetime.timedelta(hours=1)
        )
        self.account.refresh_from_db()

    def _connected(self) -> bool:
        return GoogleCalendarAccount.objects.filter(pk=self.account.pk).exists()

    def _press_export(self) -> str:
        self.client.force_login(self.user)
        response = self.client.post(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))
        return json.loads(response["HX-Trigger"])["showToast"]["message"]

    def _press_remove(self) -> str:
        self.client.force_login(self.user)
        response = self.client.delete(reverse("trips.calendar.export", kwargs={"trip_slug": self.trip.slug}))
        return json.loads(response["HX-Trigger"])["showToast"]["message"]

    def _api_export(self) -> Any:
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


class ATokenRefreshGoogleCouldNotAnswerIsBusyTests(_ExpiredTokenCase):
    def test_the_export_button_says_busy_keeps_the_connection_and_writes_nothing(self) -> None:
        for label, answer in _TOKEN_BUSY.items():
            with self.subTest(label):
                self.google.token_answer = answer

                message = self._press_export()

                self.assertEqual(message, calendar_sync.CALENDAR_BUSY_MESSAGE)
                self.assertTrue(self._connected())
                self.assertEqual(self.google.requests, [])

    def test_the_api_answers_503_with_retry_after_and_keeps_the_connection(self) -> None:
        self.google.token_answer = (503, {"error": "temporarily_unavailable"})

        response = self._api_export()

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        self.assertTrue(self._connected())

    def test_removing_the_trip_says_busy_and_keeps_the_connection(self) -> None:
        self.google.token_answer = requests.ConnectionError("connection refused")
        TripCalendarLink.objects.create(
            trip=self.trip, profile=self.profile, google_event_id="evt-trip", direction="exported"
        )

        message = self._press_remove()

        self.assertEqual(message, calendar_sync.CALENDAR_BUSY_MESSAGE)
        self.assertTrue(self._connected())
        self.assertTrue(TripCalendarLink.objects.filter(trip=self.trip, profile=self.profile).exists())

    def test_the_import_dialog_the_preview_and_the_background_import_keep_the_connection(self) -> None:
        self.google.token_answer = (500, {"error": "internal_failure"})
        self.client.force_login(self.user)

        dialog = self.client.get(reverse("trips.calendar.import"))
        preview = self.client.post(reverse("trips.calendar.import.preview"), {"event_ids": ["evt-1"]})
        imported = run_calendar_import(self.profile.pk, [{"event_id": "evt-1"}])

        self.assertEqual(dialog.status_code, 200)
        self.assertNotContains(dialog, "expired")
        self.assertNotIn("expired", preview.content.decode())
        self.assertNotIn("expired", imported["message"])
        self.assertTrue(self._connected())

    def test_a_push_stays_owed_and_the_sweep_finishes_it_once_google_answers(self) -> None:
        export_trip_to_calendar(self.account, self.trip)
        link = self._link(None)
        TripCalendarLink.objects.filter(pk=link.pk).update(auto_sync=True)
        self._expire_token()
        Trip.objects.filter(pk=self.trip.pk).update(name="Renamed")
        self.trip.refresh_from_db()
        self.trip.save(update_fields=["updated"])
        self.google.token_answer = requests.Timeout("read timed out")

        self.assertEqual(push_auto_synced_trip_changes(self.trip), 0)

        link.refresh_from_db()
        self.assertIsNotNone(link.push_requested_at)
        # A busy token service passes, so the push is not counted toward the cap that drops it.
        self.assertEqual(link.push_attempts, 0)
        self.assertTrue(self._connected())

        self.google.token_answer = (200, {"access_token": "refreshed", "expires_in": 3599})
        self.assertEqual(push_auto_synced_trip_changes(self.trip), 1)
        self.assertEqual(self._event(None)["summary"], "Renamed")


class GoogleRefusingTheGrantStillDropsTheConnectionTests(_ExpiredTokenCase):
    def test_invalid_grant_on_refresh_drops_the_connection(self) -> None:
        self.google.token_answer = (400, INVALID_GRANT)

        message = self._press_export()

        self.assertIn("reconnect", message)
        self.assertFalse(self._connected())

    def test_a_push_refused_for_the_grant_is_settled(self) -> None:
        export_trip_to_calendar(self.account, self.trip)
        link = self._link(None)
        TripCalendarLink.objects.filter(pk=link.pk).update(auto_sync=True, push_requested_at=timezone.now())
        self._expire_token()
        self.google.token_answer = (400, INVALID_GRANT)

        push_auto_synced_trip_changes(self.trip)

        link.refresh_from_db()
        self.assertIsNone(link.push_requested_at)


class ARefusalOfTheSiteIsNotTheUsersTests(_ExpiredTokenCase):
    def test_a_refused_oauth_client_is_logged_as_an_error_and_reported_as_unavailable(self) -> None:
        for label, answer in _TOKEN_CLIENT_REFUSALS.items():
            with self.subTest(label):
                self.google.token_answer = answer

                with self.assertLogs(google_oauth.logger, "ERROR"):
                    message = self._press_export()

                self.assertEqual(message, calendar_sync.CALENDAR_UNAVAILABLE_MESSAGE)
                self.assertTrue(self._connected())

    def test_a_site_with_no_oauth_client_reports_unavailable_rather_than_failing(self) -> None:
        with (
            mock.patch.object(
                calendar_google, "_oauth_client", side_effect=calendar_google.CalendarNotConfiguredError("no client")
            ),
            self.assertLogs(calendar_google.logger, "ERROR"),
        ):
            message = self._press_export()

        self.assertEqual(message, calendar_sync.CALENDAR_UNAVAILABLE_MESSAGE)
        self.assertTrue(self._connected())

    def test_a_disabled_calendar_api_is_logged_as_an_error_and_keeps_every_connection(self) -> None:
        self._fresh_token()
        for label, payload in _SITE_REFUSALS.items():
            with self.subTest(label):
                self.google.refuse_next = [(403, payload)]

                with self.assertLogs(calendar_google.logger, "ERROR"):
                    message = self._press_export()

                self.assertEqual(message, calendar_sync.CALENDAR_UNAVAILABLE_MESSAGE)
                self.assertTrue(self._connected())

    def test_the_api_answers_503_for_a_disabled_calendar_api(self) -> None:
        self._fresh_token()
        self.google.refuse_next = [(403, API_DISABLED)]

        with self.assertLogs(calendar_google.logger, "ERROR"):
            response = self._api_export()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], calendar_sync.CALENDAR_UNAVAILABLE_MESSAGE)
        self.assertTrue(self._connected())

    def test_a_push_the_site_cannot_make_stays_owed(self) -> None:
        self._fresh_token()
        export_trip_to_calendar(self.account, self.trip)
        link = self._link(None)
        TripCalendarLink.objects.filter(pk=link.pk).update(
            auto_sync=True, push_requested_at=timezone.now(), event_fingerprint=""
        )
        self.google.refuse_next = [(403, API_DISABLED)]

        with self.assertLogs(calendar_google.logger, "ERROR"):
            push_auto_synced_trip_changes(self.trip)

        link.refresh_from_db()
        self.assertIsNotNone(link.push_requested_at)
        self.assertTrue(self._connected())

    def test_a_403_that_names_no_reason_keeps_the_connection(self) -> None:
        """Every refusal the Calendar API makes names a reason; a 403 naming none is a front end or proxy refusing the site."""
        self._fresh_token()
        for content in (
            b"<html><title>403 Forbidden</title></html>",
            b"",
            b'{"error": {"code": 403, "message": "Forbidden"}}',
        ):
            with self.subTest(content=content):
                with (
                    mock.patch.object(
                        GoogleCalendarGateway, "_send", return_value=_raw_response(403, content, "text/html")
                    ),
                    self.assertLogs(calendar_google.logger, "ERROR"),
                ):
                    message = self._press_export()

                self.assertEqual(message, calendar_sync.CALENDAR_UNAVAILABLE_MESSAGE)
                self.assertTrue(self._connected())

    def test_a_refusal_of_one_event_is_not_a_dead_grant(self) -> None:
        self._fresh_token()
        self.google.refuse_next = [(403, NON_ORGANIZER)]

        with self.assertRaises(GatewayRequestError) as caught:
            GoogleCalendarGateway(account=self.account).update_event("evt", {"summary": "x"})

        self.assertNotIsInstance(caught.exception, GoogleAuthExpiredError)
        self.assertNotIsInstance(caught.exception, CalendarUnavailableError)
        self.assertTrue(self._connected())


class APushGoogleCannotTakeNowIsNeverGivenUpTests(_ExpiredTokenCase):
    """Only a refusal of the push's own counts toward ``MAX_CALENDAR_PUSH_ATTEMPTS``.

    A push that would clear a hidden location must not be dropped because Google failed, rate-limited or refused the
    site for an hour. Such a push waits for the sweep, bounded only by ``MAX_OWED_CALENDAR_WRITE_AGE``.
    """

    def setUp(self) -> None:
        super().setUp()
        self._fresh_token()
        export_trip_to_calendar(self.account, self.trip)
        self.link = self._link(None)
        TripCalendarLink.objects.filter(pk=self.link.pk).update(auto_sync=True)

    def _rename(self, name: str) -> None:
        Trip.objects.filter(pk=self.trip.pk).update(name=name)
        self.trip.refresh_from_db()
        self.trip.save(update_fields=["updated"])

    def _rounds_of_push_and_sweep(self, rounds: int) -> None:
        for _round in range(rounds):
            push_auto_synced_trip_changes(self.trip)
            TripCalendarLink.objects.filter(pk=self.link.pk, push_requested_at__isnull=False).update(
                push_requested_at=timezone.now() - datetime.timedelta(hours=1)
            )
            requeue_pending_calendar_pushes()

    def test_google_failing_rate_limiting_or_refusing_the_site_never_uses_one_up(self) -> None:
        cases = {
            "Google failing": (503, _google_error(503, "backendError", "Backend Error")),
            "Google's rate limit": (403, RATE_LIMIT),
            "the site refused": (403, API_DISABLED),
        }
        for label, failure in cases.items():
            with self.subTest(label):
                self._rename(f"Renamed for {label}")
                self.google.fail_after, self.google.failure = 0, failure

                self._rounds_of_push_and_sweep(MAX_CALENDAR_PUSH_ATTEMPTS + 2)

                self.link.refresh_from_db()
                self.assertIsNotNone(self.link.push_requested_at)
                self.assertEqual(self.link.push_attempts, 0)

                self.google.fail_after = None
                self.assertEqual(push_auto_synced_trip_changes(self.trip), 1)
                self.assertEqual(self._event(None)["summary"], f"Renamed for {label}")

    def test_a_refusal_of_its_own_is_counted_and_dropped_at_the_cap(self) -> None:
        self._rename("Renamed")
        self.google.fail_after, self.google.failure = 0, (400, _google_error(400, "invalid", "Invalid value"))

        self._rounds_of_push_and_sweep(MAX_CALENDAR_PUSH_ATTEMPTS + 1)

        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)

    def test_the_sweep_drops_a_push_owed_longer_than_the_age_cap(self) -> None:
        TripCalendarLink.objects.filter(pk=self.link.pk).update(
            push_requested_at=timezone.now() - MAX_OWED_CALENDAR_WRITE_AGE - datetime.timedelta(minutes=1)
        )

        requeue_pending_calendar_pushes()

        self.link.refresh_from_db()
        self.assertIsNone(self.link.push_requested_at)


class A401IsAnsweredWithAFreshTokenFirstTests(_ExpiredTokenCase):
    """Calendar guide, 401 Invalid Credentials: get a new access token with the refresh token; only if that fails, re-authorize."""

    def setUp(self) -> None:
        super().setUp()
        self._fresh_token()

    def test_a_401_cured_by_a_fresh_token_is_retried_once_and_succeeds(self) -> None:
        self.google.refuse_next = [(401, INVALID_CREDENTIALS)]

        result = export_trip_to_calendar(self.account, self.trip)

        self.assertTrue(result.complete)
        self.assertEqual(self.google.token_requests, 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.access_token, "refreshed")
        self.assertTrue(self._connected())

    def test_a_401_a_fresh_token_does_not_cure_is_a_dead_grant(self) -> None:
        self.google.refuse_next = [(401, INVALID_CREDENTIALS), (401, INVALID_CREDENTIALS)]

        message = self._press_export()

        self.assertIn("reconnect", message)
        self.assertEqual(self.google.token_requests, 1)
        self.assertFalse(self._connected())

    def test_a_401_whose_refresh_google_cannot_answer_is_busy(self) -> None:
        self.google.refuse_next = [(401, INVALID_CREDENTIALS)]
        self.google.token_answer = (503, {"error": "temporarily_unavailable"})

        message = self._press_export()

        self.assertEqual(message, calendar_sync.CALENDAR_BUSY_MESSAGE)
        self.assertTrue(self._connected())

    def test_a_scope_refusal_is_still_a_dead_grant_without_a_refresh(self) -> None:
        self.google.refuse_next = [(403, INSUFFICIENT_SCOPE)]

        message = self._press_export()

        self.assertIn("reconnect", message)
        self.assertEqual(self.google.token_requests, 0)
        self.assertFalse(self._connected())


class TokenEndpointAnswerTests(SimpleTestCase):
    """``google_oauth.refresh_access_token`` sorts the token endpoint's answers by whose problem they are."""

    def _refresh(self, answer: requests.Response | Exception) -> dict[str, Any]:
        effect = answer if isinstance(answer, Exception) else None
        with mock.patch.object(google_oauth.requests, "post", return_value=answer, side_effect=effect):
            return google_oauth.refresh_access_token("client", "secret", "refresh")

    def test_a_200_returns_the_token(self) -> None:
        self.assertEqual(
            self._refresh(_raw_response(200, b'{"access_token": "a", "expires_in": 3599}'))["access_token"], "a"
        )

    def test_answers_that_pass_are_busy(self) -> None:
        for label, answer in _TOKEN_BUSY.items():
            with self.subTest(label):
                response = (
                    answer
                    if isinstance(answer, Exception)
                    else _raw_response(answer[0], json.dumps(answer[1]).encode())
                )
                with self.assertRaises(GoogleTokenServiceBusyError):
                    self._refresh(response)

    def test_a_200_without_a_token_is_busy_not_a_crash(self) -> None:
        for content in (b"", b"<html></html>", b"{}", b'{"access_token": ""}'):
            with self.subTest(content=content), self.assertRaises(GoogleTokenServiceBusyError):
                self._refresh(_raw_response(200, content))

    def test_a_revoked_or_expired_grant_is_a_dead_grant(self) -> None:
        for body in (
            INVALID_GRANT,
            {"error": "invalid_grant", "error_subtype": "invalid_rapt"},
            {"error": "admin_policy_enforced"},
        ):
            with self.subTest(body=body), self.assertRaises(GoogleAuthExpiredError):
                self._refresh(_raw_response(400, json.dumps(body).encode()))

    def test_a_refusal_of_the_client_or_the_request_is_the_sites(self) -> None:
        cases = [
            *(_raw_response(status, json.dumps(body).encode()) for status, body in _TOKEN_CLIENT_REFUSALS.values()),
            _raw_response(400, b'{"error": "invalid_request"}'),
            _raw_response(400, b"<html>Bad Request</html>"),
        ]
        for response in cases:
            with (
                self.subTest(content=response.content),
                self.assertLogs(google_oauth.logger, "ERROR"),
                self.assertRaises(GoogleOAuthClientRefusedError),
            ):
                self._refresh(response)

    def test_the_errors_are_ordered_so_no_caller_mistakes_one_for_another(self) -> None:
        self.assertFalse(issubclass(GoogleTokenServiceBusyError, GoogleAuthExpiredError))
        self.assertFalse(issubclass(GoogleOAuthClientRefusedError, GoogleAuthExpiredError))
        self.assertTrue(issubclass(CalendarBusyError, calendar_google.RateLimitExceededError))
        self.assertFalse(issubclass(CalendarUnavailableError, GoogleAuthExpiredError))


class SiteRefusalTests(SimpleTestCase):
    def test_a_refusal_of_the_sites_google_project_is_recognised(self) -> None:
        for label, payload in _SITE_REFUSALS.items():
            with self.subTest(label):
                self.assertTrue(is_site_refusal(_raw_response(403, json.dumps(payload).encode())))

    def test_the_users_refusals_and_rate_limits_are_not(self) -> None:
        for payload in (
            INSUFFICIENT_SCOPE,
            _google_error(403, "forbidden", "Forbidden", domain="global"),
            _google_error(403, "rateLimitExceeded", "Rate Limit Exceeded"),
            NON_ORGANIZER,
        ):
            with self.subTest(payload=payload):
                self.assertFalse(is_site_refusal(_raw_response(403, json.dumps(payload).encode())))

    def test_a_body_without_a_readable_reason_is_not(self) -> None:
        for content in (
            b"",
            b"<html>403</html>",
            b'{"error": "forbidden"}',
            b'{"error": {"details": [{"reason": 7}]}}',
        ):
            with self.subTest(content=content):
                self.assertFalse(is_site_refusal(_raw_response(403, content)))
