"""Google Calendar API gateway and OAuth helpers.

All calls operate on *one user's own calendar* using tokens from that user's
:class:`~urbanlens.dashboard.models.calendar_sync.GoogleCalendarAccount` row -
there is no site-wide calendar or service account. The site's Google OAuth
client (``UL_GOOGLE_CLIENT_ID`` / ``UL_GOOGLE_CLIENT_SECRET``) is only the
application identity; each user grants it access to their calendar via the
"Connect Google Calendar" consent flow.

Module-level helpers wrap the provider-agnostic OAuth mechanics in
``dashboard/services/google_oauth.py`` with Calendar's own client lookup and
scopes, preserving their original (pre-extraction) signatures so every
existing caller keeps working unchanged. The :class:`GoogleCalendarGateway`
wraps the Calendar v3 events API with automatic token refresh and the
standard rate-limit/call-log session via ``service_key``.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.utils import timezone

from urbanlens.dashboard.services import google_oauth
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError
from urbanlens.dashboard.services.google_oauth import GoogleAuthExpiredError, extract_email_from_id_token
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

# calendar.events grants read/write on events only (not calendar settings);
# openid+email let us show which Google account is connected.
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "email",
)

# Private extended property stamped on every event UrbanLens exports, so
# imports can recognise (and skip) events that originated as trips.
TRIP_UUID_EVENT_PROPERTY = "urbanlens_trip_uuid"
# Additionally stamped on per-activity events so they can be told apart from
# the trip-level all-day event.
ACTIVITY_ID_EVENT_PROPERTY = "urbanlens_activity_id"


class CalendarNotConfiguredError(google_oauth.GoogleOAuthNotConfiguredError):
    """Raised when the site has no Google OAuth client configured."""


def _oauth_client() -> tuple[str, str]:
    """Return the site's Google OAuth client id and secret.

    Returns:
        Tuple of (client_id, client_secret).

    Raises:
        CalendarNotConfiguredError: When either value is missing.
    """
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    if not client_id or not client_secret:
        raise CalendarNotConfiguredError(
            "Google Calendar integration requires UL_GOOGLE_CLIENT_ID and UL_GOOGLE_CLIENT_SECRET.",
        )
    return client_id, client_secret


def build_authorization_url(redirect_uri: str, state: str) -> str:
    """Build the Google consent-screen URL for the calendar connect flow.

    Args:
        redirect_uri: Absolute callback URL registered with the OAuth client.
        state: Signed opaque state token, verified on callback.

    Returns:
        Fully-formed authorization URL to redirect the user to.

    Raises:
        CalendarNotConfiguredError: When the OAuth client is not configured.
    """
    client_id, _ = _oauth_client()
    return google_oauth.build_authorization_url(client_id, redirect_uri, CALENDAR_SCOPES, state)


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for access/refresh tokens.

    Args:
        code: Authorization code from the OAuth callback.
        redirect_uri: The same redirect URI used to obtain the code.

    Returns:
        Token response payload (``access_token``, ``refresh_token``,
        ``expires_in``, ``id_token``, ``scope``, ...).

    Raises:
        CalendarNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the token exchange fails.
    """
    client_id, client_secret = _oauth_client()
    return google_oauth.exchange_code_for_tokens(client_id, client_secret, code, redirect_uri)


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Obtain a fresh access token using a refresh token.

    Args:
        refresh_token: The stored OAuth refresh token.

    Returns:
        Token response payload (``access_token``, ``expires_in``, ...).

    Raises:
        CalendarNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the refresh fails (e.g. access revoked).
    """
    client_id, client_secret = _oauth_client()
    return google_oauth.refresh_access_token(client_id, client_secret, refresh_token)


def revoke_token(token: str) -> bool:
    """Best-effort revocation of an access or refresh token at Google.

    Args:
        token: The token to revoke (refresh token revokes the whole grant).

    Returns:
        True when Google confirmed the revocation.
    """
    return google_oauth.revoke_token(token)


class CalendarEventNotFoundError(GatewayRequestError):
    """Raised when a referenced calendar event no longer exists."""


@dataclass(slots=True, kw_only=True)
class GoogleCalendarGateway(Gateway):
    """Events API client bound to one user's connected Google account.

    Attributes:
        account: The user's stored calendar credentials. Tokens are
            refreshed in place (and persisted) as needed.
        base_url: Calendar v3 API root.
    """

    service_key: ClassVar[str] = "google_calendar"
    paid_service: ClassVar[bool] = False

    account: GoogleCalendarAccount
    base_url: str = CALENDAR_API_BASE

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)

    @property
    def _events_url(self) -> str:
        return f"{self.base_url}/calendars/{self.account.calendar_id}/events"

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization headers, refreshing the access token first if needed.

        Returns:
            Headers dict with a valid bearer token.

        Raises:
            GatewayRequestError: When the token cannot be refreshed.
        """
        if self.account.is_token_expired:
            self._refresh_token()
        return {"Authorization": f"Bearer {self.account.access_token}"}

    def _refresh_token(self) -> None:
        """Refresh and persist the account's access token.

        Raises:
            GoogleAuthExpiredError: When no refresh token is stored or the
                refresh is rejected by Google.
        """
        if not self.account.refresh_token:
            raise GoogleAuthExpiredError("Google Calendar connection is missing a refresh token. Please reconnect.")
        payload = refresh_access_token(self.account.refresh_token)
        self.account.access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in") or 3600)
        self.account.token_expiry = timezone.now() + datetime.timedelta(seconds=expires_in)
        # Google occasionally rotates the refresh token as well.
        if payload.get("refresh_token"):
            self.account.refresh_token = payload["refresh_token"]
        self.account.save(update_fields=["access_token", "refresh_token", "token_expiry", "updated"])

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> dict[str, Any] | None:
        """Perform an authenticated request against the Calendar API.

        Args:
            method: HTTP method.
            url: Absolute URL.
            params: Optional query parameters.
            json_body: Optional JSON request body.
            ok_statuses: Statuses treated as success.

        Returns:
            Decoded JSON body, or None for empty (204) responses.

        Raises:
            GoogleAuthExpiredError: When Google rejects the current credentials (401/403).
            GatewayRequestError: On any other non-success response.
        """
        response = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=self._auth_headers(),
            timeout=30,
        )
        if response.status_code in ok_statuses:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        logger.warning(
            "Google Calendar API %s %s failed (%s): %s",
            method,
            url,
            response.status_code,
            response.text[:500],
        )
        if response.status_code in (401, 403):
            raise GoogleAuthExpiredError("Google Calendar access was denied. Please reconnect your account.")
        if response.status_code == 404:
            raise CalendarEventNotFoundError("Calendar event not found.")
        raise GatewayRequestError(f"Google Calendar API request failed with status {response.status_code}.")

    def list_events(
        self,
        *,
        time_min: datetime.datetime,
        time_max: datetime.datetime | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List (non-recurring-expanded) upcoming events on the user's calendar.

        Args:
            time_min: Lower bound (inclusive) for the event end time.
            time_max: Optional upper bound for the event start time.
            max_results: Page size cap; a single page is fetched.

        Returns:
            Event resource dicts ordered by start time.
        """
        params: dict[str, Any] = {
            "timeMin": time_min.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max_results,
        }
        if time_max is not None:
            params["timeMax"] = time_max.isoformat()
        body = self._request("GET", self._events_url, params=params)
        return list(body.get("items", [])) if body else []

    def get_event(self, event_id: str) -> dict[str, Any]:
        """Fetch a single event by id.

        Args:
            event_id: Google event identifier.

        Returns:
            The event resource dict.

        Raises:
            CalendarEventNotFoundError: When the event does not exist.
            GatewayRequestError: On other API failures.
        """
        body = self._request("GET", f"{self._events_url}/{event_id}")
        if body is None:
            raise GatewayRequestError("Google Calendar returned an empty event.")
        return body

    def create_event(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create an event on the user's calendar.

        Args:
            body: Event resource payload.

        Returns:
            The created event resource dict.

        Raises:
            GatewayRequestError: On API failure.
        """
        created = self._request("POST", self._events_url, json_body=body)
        if created is None:
            raise GatewayRequestError("Google Calendar returned an empty response for event creation.")
        return created

    def update_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update an existing event (PATCH semantics).

        Args:
            event_id: Google event identifier.
            body: Partial event resource payload.

        Returns:
            The updated event resource dict.

        Raises:
            CalendarEventNotFoundError: When the event no longer exists.
            GatewayRequestError: On other API failures.
        """
        updated = self._request("PATCH", f"{self._events_url}/{event_id}", json_body=body)
        if updated is None:
            raise GatewayRequestError("Google Calendar returned an empty response for event update.")
        return updated

    def delete_event(self, event_id: str) -> None:
        """Delete an event from the user's calendar.

        Deleting an already-deleted event is treated as success.

        Args:
            event_id: Google event identifier.

        Raises:
            GatewayRequestError: On API failure other than 404/410.
        """
        try:
            self._request("DELETE", f"{self._events_url}/{event_id}", ok_statuses=(200, 204, 404, 410))
        except CalendarEventNotFoundError:
            return
