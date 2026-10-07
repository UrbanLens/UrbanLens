"""Google Calendar API gateway and OAuth helpers.
The site's Google OAuth client (``UL_GOOGLE_CLIENT_ID`` / ``UL_GOOGLE_CLIENT_SECRET``) is only the application identity; each user grants it access to their calendar via the "Connect Google Calendar" consent flow."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import datetime
import hashlib
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.utils import timezone
import requests

from urbanlens.dashboard.services.auth import google_oauth
from urbanlens.dashboard.services.auth.google_oauth import GoogleAuthExpiredError
from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_DEFAULT_SECONDS, Gateway, GatewayRateLimitedError, GatewayRequestError, UpstreamBusyError, upstream_retry_after
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
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

#: Google's own ceiling on ``maxResults`` for ``events.list``.
EVENTS_PAGE_SIZE = 250

#: Error ``reason`` values for a rate or usage limit, which Google answers with 403 as well as 429. The Calendar API's
#: error guide names ``rateLimitExceeded``, ``userRateLimitExceeded`` and ``quotaExceeded``; ``dailyLimitExceeded`` is
#: Google's general daily-quota reason, and ``RATE_LIMIT_EXCEEDED`` the ``details[].reason`` of its newer envelope.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED"})


def client_event_id(*parts: object) -> str:
    """An event id for ``events.insert`` that the same *parts* always reproduce.

    Google takes a client-assigned id of 5 to 1024 base32hex characters (``a``-``v``, ``0``-``9``), unique per
    calendar. A retry that sends the same id after a lost response is answered 409 rather than creating a
    second event.

    Args:
        *parts: What identifies the event; joined in order, so ``("a", "bc")`` and ``("ab", "c")`` differ.

    Returns:
        52 lowercase base32hex characters: a SHA-256 digest of the parts, unpadded.
    """
    seed = "\x1f".join(str(part) for part in parts)
    return base64.b32hexencode(hashlib.sha256(seed.encode()).digest()).decode().rstrip("=").lower()


@dataclass(frozen=True)
class EventListing:
    """Events read from a calendar, and whether more existed past the requested limit.

    Attributes:
        events: Event resource dicts in calendar order.
        truncated: True when the calendar held more events than were read.
    """

    events: list[dict[str, Any]]
    truncated: bool = False


def is_rate_limit_refusal(response: requests.Response) -> bool:
    """Whether Google refused a call for a rate or usage limit, which passes, rather than for the grant, which does not.

    Google gives a rate limit as 429, or as 403 with a usage-limit ``reason``. Every other 403 (``forbidden``,
    ``insufficientPermissions``, a body with no readable reason) is not one. The envelope's ``status`` is not read:
    Google sends PERMISSION_DENIED with a per-user quota refusal too.

    Args:
        response: A Calendar API response.

    Returns:
        True for a 429, or a 403 naming a reason in :data:`RATE_LIMIT_REASONS`.
    """
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    entries: list[object] = []
    for key in ("errors", "details"):
        listed = error.get(key)
        if isinstance(listed, list):
            entries.extend(listed)
    return any(isinstance(entry, dict) and isinstance(entry.get("reason"), str) and entry["reason"] in RATE_LIMIT_REASONS for entry in entries)


class CalendarRateLimitedError(RateLimitExceededError, UpstreamBusyError, GatewayRateLimitedError):
    """Google refused a call for a rate or usage limit: the grant is sound, and a later attempt may pass.

    A :class:`RateLimitExceededError`, so an export it cuts short owes a push the way one our own budget cuts short
    does, and every caller that answers "busy" for our budget answers the same for Google's.

    Args:
        service: The rate-limiter service key.
        retry_after: Seconds Google asked callers to wait, or the default when it named none.
    """

    def __init__(self, service: str, *, retry_after: int) -> None:
        super().__init__(service, f"Google refused '{service}' for a rate or usage limit")
        self.retry_after = retry_after


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
        CalendarNotConfiguredError: When the OAuth client is not configured."""
    client_id, _ = _oauth_client()
    return google_oauth.build_authorization_url(client_id, redirect_uri, CALENDAR_SCOPES, state)


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for access/refresh tokens.

    Args:
        code: Authorization code from the OAuth callback.
        redirect_uri: The same redirect URI used to obtain the code.

    Returns:
        Token response payload (``access_token``, ``refresh_token``, ``expires_in``, ``id_token``, ``scope``, ...).

    Raises:
        CalendarNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the token exchange fails."""
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
        GatewayRequestError: When the refresh fails (e.g. access revoked)."""
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


class CalendarEventExistsError(GatewayRequestError):
    """Raised when an event created with a client-assigned id finds that id already taken on the calendar."""


@dataclass(slots=True, kw_only=True)
class GoogleCalendarGateway(Gateway):
    """Events API client bound to one user's connected Google account.

    Attributes:
        account: The user's stored calendar credentials.
        base_url: Calendar v3 API root."""

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
            EnvironmentRefusedError: This environment does not call the service; asked before the token refresh, which
                would otherwise reach Google for a call that is then refused (D26).
            GatewayRequestError: When the token cannot be refreshed."""
        from urbanlens.dashboard.services.core.egress import require_egress

        require_egress(type(self).service_key)
        if self.account.is_token_expired:
            self._refresh_token()
        return {"Authorization": f"Bearer {self.account.access_token}"}

    def _refresh_token(self) -> None:
        """Refresh and persist the account's access token.

        Raises:
            GoogleAuthExpiredError: When no refresh token is stored or the refresh is rejected by Google."""
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
            CalendarRateLimitedError: Google refused for a rate or usage limit (429, or 403 naming one).
            GoogleAuthExpiredError: When Google rejects the current credentials (401, or any other 403).
            CalendarEventNotFoundError: The event does not exist, or was deleted (404/410).
            CalendarEventExistsError: The client-assigned id of an event being created is taken (409).
            GatewayRequestError: Any other failure, including no response at all.
        """
        headers = self._auth_headers()
        try:
            response = self.session.request(method, url, params=params, json=json_body, headers=headers, timeout=30)
        except requests.RequestException as exc:
            # A write may have reached Google even though its answer did not; callers retry with the same event id.
            logger.warning("Google Calendar API %s %s got no response: %s", method, url, type(exc).__name__)
            raise GatewayRequestError("Google Calendar did not answer.") from exc
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
        if is_rate_limit_refusal(response):
            raise CalendarRateLimitedError(type(self).service_key, retry_after=upstream_retry_after(response) or UPSTREAM_BUSY_DEFAULT_SECONDS)
        if response.status_code in (401, 403):
            raise GoogleAuthExpiredError("Google Calendar access was denied. Please reconnect your account.")
        if response.status_code in (404, 410):
            raise CalendarEventNotFoundError("Calendar event not found.")
        if response.status_code == 409:
            raise CalendarEventExistsError("A calendar event with this id already exists.")
        raise GatewayRequestError(f"Google Calendar API request failed with status {response.status_code}.")

    def list_events(
        self,
        *,
        time_min: datetime.datetime,
        time_max: datetime.datetime | None = None,
        limit: int,
    ) -> EventListing:
        """List (recurring-expanded) upcoming events on the user's calendar, following pages up to *limit*.

        Args:
            time_min: Lower bound (inclusive) for the event end time.
            time_max: Optional upper bound for the event start time.
            limit: The most events to read; one more is asked for so a cut-off is reported, not silent.

        Returns:
            The events ordered by start time, and whether the calendar held more.
        """
        events: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "timeMin": time_min.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": min(EVENTS_PAGE_SIZE, limit - len(events) + 1),
            }
            if time_max is not None:
                params["timeMax"] = time_max.isoformat()
            if page_token:
                params["pageToken"] = page_token
            body = self._request("GET", self._events_url, params=params) or {}
            events.extend(body.get("items") or [])
            if len(events) > limit:
                return EventListing(events[:limit], truncated=True)
            page_token = body.get("nextPageToken") or ""
            if not page_token:
                return EventListing(events)

    def get_event(self, event_id: str) -> dict[str, Any]:
        """Fetch a single event by id.

        Args:
            event_id: Google event identifier.

        Returns:
            The event resource dict.

        Raises:
            CalendarEventNotFoundError: When the event does not exist.
        """
        body = self._request("GET", f"{self._events_url}/{event_id}")
        if body is None:
            raise GatewayRequestError("Google Calendar returned an empty event.")
        return body

    def create_event(self, body: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
        """Create an event on the user's calendar.

        Args:
            body: Event resource payload.
            event_id: A client-assigned id (:func:`client_event_id`), so a retried create cannot make a second event;
                None lets Google choose one.

        Returns:
            The created event resource dict.

        Raises:
            CalendarEventExistsError: *event_id* is already taken on this calendar, including by a deleted event.
            GatewayRequestError: On API failure.
        """
        created = self._request("POST", self._events_url, json_body={**body, "id": event_id} if event_id else body)
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
        """
        updated = self._request("PATCH", f"{self._events_url}/{event_id}", json_body=body)
        if updated is None:
            raise GatewayRequestError("Google Calendar returned an empty response for event update.")
        return updated

    def delete_event(self, event_id: str) -> None:
        """Delete an event from the user's calendar.

        Args:
            event_id: Google event identifier.

        Raises:
            GatewayRequestError: On API failure other than 404/410.
        """
        try:
            self._request("DELETE", f"{self._events_url}/{event_id}", ok_statuses=(200, 204, 404, 410))
        except CalendarEventNotFoundError:
            return
