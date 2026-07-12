"""Google Photos Picker API gateway.

All calls operate on *one user's own* Google Photos library using tokens from
that user's :class:`~urbanlens.dashboard.models.google_photos.GooglePhotosAccount`
row - there is no site-wide grant. Google's Photos Library API stopped
exposing GPS coordinates and broad library search entirely (see
``docs/plugins.md`` / this session's Immich research); the **Picker API** is
the only sanctioned mechanism left, and it's fundamentally a different shape:
the user picks photos in Google's own UI (``pickerUri``), we poll a session
until they're done, then list whatever they picked - there is no server-side
"near this pin" filter to apply.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.utils import timezone

from urbanlens.dashboard.services import google_oauth
from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount

logger = logging.getLogger(__name__)

PICKER_API_BASE = "https://photospicker.googleapis.com/v1"
PHOTOS_PICKER_SCOPES = ("https://www.googleapis.com/auth/photospicker.mediaitems.readonly", "openid", "email")

_REQUEST_TIMEOUT = 30
_DEFAULT_POLL_INTERVAL_S = 5
_DEFAULT_TIMEOUT_S = 300


class GooglePhotosNotConfiguredError(google_oauth.GoogleOAuthNotConfiguredError):
    """Raised when the site has no Google OAuth client configured."""


def _oauth_client() -> tuple[str, str]:
    """Return the site's Google OAuth client id and secret.

    Reuses the same site-wide client as Calendar (``UL_GOOGLE_CLIENT_ID``/
    ``UL_GOOGLE_CLIENT_SECRET``) - only the requested scopes differ per feature.

    Returns:
        Tuple of (client_id, client_secret).

    Raises:
        GooglePhotosNotConfiguredError: When either value is missing.
    """
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    if not client_id or not client_secret:
        raise GooglePhotosNotConfiguredError("Google Photos integration requires UL_GOOGLE_CLIENT_ID and UL_GOOGLE_CLIENT_SECRET.")
    return client_id, client_secret


def build_authorization_url(redirect_uri: str, state: str) -> str:
    """Build the Google consent-screen URL for the Google Photos connect flow.

    Args:
        redirect_uri: Absolute callback URL registered with the OAuth client.
        state: Signed opaque state token, verified on callback.

    Returns:
        Fully-formed authorization URL to redirect the user to.

    Raises:
        GooglePhotosNotConfiguredError: When the OAuth client is not configured.
    """
    client_id, _ = _oauth_client()
    return google_oauth.build_authorization_url(client_id, redirect_uri, PHOTOS_PICKER_SCOPES, state)


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for access/refresh tokens.

    Args:
        code: Authorization code from the OAuth callback.
        redirect_uri: The same redirect URI used to obtain the code.

    Returns:
        Token response payload.

    Raises:
        GooglePhotosNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the token exchange fails.
    """
    client_id, client_secret = _oauth_client()
    return google_oauth.exchange_code_for_tokens(client_id, client_secret, code, redirect_uri)


def revoke_token(token: str) -> bool:
    """Best-effort revocation of an access or refresh token at Google.

    Args:
        token: The token to revoke.

    Returns:
        True when Google confirmed the revocation.
    """
    return google_oauth.revoke_token(token)


def _parse_duration_seconds(value: str | None, default: int) -> int:
    """Parse a Google API duration string (e.g. ``"5s"``) into whole seconds.

    Args:
        value: The duration string, or None.
        default: Fallback when ``value`` is missing or unparsable.

    Returns:
        Whole seconds.
    """
    if not value:
        return default
    try:
        return max(1, int(float(value.rstrip("s"))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class PickerSession:
    """State of a Photos Picker session."""

    id: str
    picker_uri: str
    media_items_set: bool
    poll_interval_s: int
    timeout_s: int


@dataclass(frozen=True, slots=True)
class PickedMediaItem:
    """One media item the user selected in the Google Photos picker UI."""

    id: str
    base_url: str
    mime_type: str
    filename: str
    create_time: datetime.datetime | None


@dataclass(slots=True, kw_only=True)
class GooglePhotosGateway(Gateway):
    """Picker API client bound to one user's connected Google Photos account.

    Attributes:
        account: The user's stored Google Photos OAuth credentials. Tokens
            are refreshed in place (and persisted) as needed.
    """

    service_key: ClassVar[str] = "google_photos"
    paid_service: ClassVar[bool] = False

    account: GooglePhotosAccount

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)

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
            GatewayRequestError: When no refresh token is stored or the
                refresh is rejected by Google.
        """
        if not self.account.refresh_token:
            raise GatewayRequestError("Google Photos connection is missing a refresh token. Please reconnect.")
        client_id, client_secret = _oauth_client()
        payload = google_oauth.refresh_access_token(client_id, client_secret, self.account.refresh_token)
        self.account.access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in") or 3600)
        self.account.token_expiry = timezone.now() + datetime.timedelta(seconds=expires_in)
        if payload.get("refresh_token"):
            self.account.refresh_token = payload["refresh_token"]
        self.account.save(update_fields=["access_token", "refresh_token", "token_expiry", "updated"])

    def create_session(self) -> PickerSession:
        """Create a new picker session for the user to select photos in.

        Returns:
            The new session, including the ``picker_uri`` to send the user to.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        response = self.session.post(f"{PICKER_API_BASE}/sessions", json={}, headers=self._auth_headers(), timeout=_REQUEST_TIMEOUT)
        if not response.ok:
            logger.warning("Google Photos create_session failed (%s): %s", response.status_code, response.text[:500])
            raise GatewayRequestError(f"Could not start a Google Photos picker session (status {response.status_code}).")
        return self._session_from_json(response.json())

    def get_session(self, session_id: str) -> PickerSession:
        """Fetch the current state of a picker session.

        Args:
            session_id: The session id from :meth:`create_session`.

        Returns:
            The session's current state.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        response = self.session.get(f"{PICKER_API_BASE}/sessions/{session_id}", headers=self._auth_headers(), timeout=_REQUEST_TIMEOUT)
        if not response.ok:
            logger.warning("Google Photos get_session failed (%s): %s", response.status_code, response.text[:500])
            raise GatewayRequestError(f"Could not check the Google Photos picker session (status {response.status_code}).")
        return self._session_from_json(response.json())

    def _session_from_json(self, body: dict[str, Any]) -> PickerSession:
        polling = body.get("pollingConfig", {})
        return PickerSession(
            id=body["id"],
            picker_uri=body["pickerUri"],
            media_items_set=bool(body.get("mediaItemsSet")),
            poll_interval_s=_parse_duration_seconds(polling.get("pollInterval"), _DEFAULT_POLL_INTERVAL_S),
            timeout_s=_parse_duration_seconds(polling.get("timeoutIn"), _DEFAULT_TIMEOUT_S),
        )

    def list_session_media_items(self, session_id: str) -> list[PickedMediaItem]:
        """List every item the user selected in a completed picker session.

        Args:
            session_id: The session id from :meth:`create_session`.

        Returns:
            The picked media items, across all pages.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        items: list[PickedMediaItem] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"sessionId": session_id, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            response = self.session.get(f"{PICKER_API_BASE}/mediaItems", params=params, headers=self._auth_headers(), timeout=_REQUEST_TIMEOUT)
            if not response.ok:
                logger.warning("Google Photos list_session_media_items failed (%s): %s", response.status_code, response.text[:500])
                raise GatewayRequestError(f"Could not list picked Google Photos items (status {response.status_code}).")
            body = response.json()
            for raw in body.get("mediaItems", []):
                media_file = raw.get("mediaFile", {})
                items.append(
                    PickedMediaItem(
                        id=raw["id"],
                        base_url=media_file["baseUrl"],
                        mime_type=media_file.get("mimeType", "application/octet-stream"),
                        filename=media_file.get("filename", f"{raw['id']}.jpg"),
                        create_time=_parse_timestamp(raw.get("createTime")),
                    ),
                )
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return items

    def download_media_item(self, base_url: str, *, original: bool = True) -> bytes:
        """Download a picked item's bytes.

        Args:
            base_url: The item's ``base_url`` from :meth:`list_session_media_items`.
            original: When True (default), request the original file (``=d``
                suffix per Google's documented download convention); when
                False, request a reasonably large preview instead.

        Returns:
            The file bytes.

        Raises:
            GatewayRequestError: On a network error or non-2xx response.
        """
        suffix = "=d" if original else "=w2048-h2048"
        response = self.session.get(f"{base_url}{suffix}", headers=self._auth_headers(), timeout=_REQUEST_TIMEOUT)
        if not response.ok:
            raise GatewayRequestError(f"Downloading the Google Photos item failed (status {response.status_code}).")
        return response.content


def session_items_cache_key(session_id: str) -> str:
    """Cache key holding a picker session's listed media items (id -> base_url/mime_type/filename).

    Shared between the controller (writes it after listing, reads it for the
    thumbnail proxy) and the import task (reads it to resolve each selected
    item's download URL), so both agree on the same key format.

    Args:
        session_id: The picker session id.

    Returns:
        The cache key.
    """
    return f"ul_gphotos_session_items_{session_id}"


def media_item_web_url(media_item_id: str) -> str:
    """Return the Google Photos web URL for one media item.

    Used both as the "view on Google Photos" attribution link and as the
    de-dup key stored on ``Image.source_url`` - an item already imported to a
    pin is recognised by matching this URL, without re-fetching it. Unlike
    Immich/Flickr, Google Photos gives no per-account context needed to build
    this - the Picker API's media item id is the same id used in this URL
    scheme.

    Args:
        media_item_id: The Picker API media item id.

    Returns:
        The item's URL in the Google Photos web UI.
    """
    return f"https://photos.google.com/lr/photo/{media_item_id}"


def _parse_timestamp(value: str | None) -> datetime.datetime | None:
    """Parse an RFC3339 timestamp string from the API, if present.

    Args:
        value: The timestamp string, or None.

    Returns:
        A timezone-aware datetime, or None when unparsable/absent.
    """
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
