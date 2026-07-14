"""Flickr OAuth 1.0a (3-legged) authorization flow.

Flickr has no OAuth2 option for accessing a user's private photos, so this is
the one integration in the app that needs the older 3-legged dance:

1. :func:`start_authorization` fetches a *temporary* request token and
   returns the URL to send the user to. The temporary token's secret is
   needed again in step 3 but Flickr's callback only echoes back the token
   itself (no generic ``state`` passthrough like OAuth2), so the caller must
   stash ``(oauth_token -> oauth_token_secret)`` somewhere keyed by the token
   (short-TTL cache) and look it up again on callback.
2. The user approves on Flickr's site; Flickr redirects back with the same
   ``oauth_token`` plus an ``oauth_verifier``.
3. :func:`finish_authorization` exchanges the temporary token + verifier for
   the permanent access token pair, using the secret stashed in step 1.
"""

from __future__ import annotations

from dataclasses import dataclass

from requests_oauthlib import OAuth1Session

from urbanlens.dashboard.services.gateway import GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

REQUEST_TOKEN_URL = "https://www.flickr.com/services/oauth/request_token"  # noqa: S105 # nosec B105 - OAuth endpoint URL, not a credential
AUTHORIZE_URL = "https://www.flickr.com/services/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.flickr.com/services/oauth/access_token"  # noqa: S105 # nosec B105 - OAuth endpoint URL, not a credential

_OAUTH_TIMEOUT = 30


class FlickrNotConfiguredError(RuntimeError):
    """Raised when the site has no Flickr API key/secret configured."""


def _consumer_credentials() -> tuple[str, str]:
    """Return the site's Flickr API key and secret.

    Returns:
        Tuple of (api_key, api_secret).

    Raises:
        FlickrNotConfiguredError: When either value is missing.
    """
    api_key = settings.flickr_api_key
    api_secret = settings.flickr_api_secret
    if not api_key or not api_secret:
        raise FlickrNotConfiguredError("Flickr integration requires UL_FLICKR_API_KEY and UL_FLICKR_API_SECRET.")
    return api_key, api_secret


def is_configured() -> bool:
    """Return whether the site has a Flickr API key and secret configured.

    Returns:
        True when both ``UL_FLICKR_API_KEY`` and ``UL_FLICKR_API_SECRET`` are set.
    """
    return bool(settings.flickr_api_key and settings.flickr_api_secret)


@dataclass(frozen=True, slots=True)
class PendingFlickrAuthorization:
    """Temporary request-token state to stash between step 1 and step 3."""

    authorization_url: str
    oauth_token: str
    oauth_token_secret: str


@dataclass(frozen=True, slots=True)
class FlickrAccessGrant:
    """The permanent per-user credential Flickr issues at the end of the flow."""

    oauth_token: str
    oauth_token_secret: str
    user_nsid: str
    username: str | None


def start_authorization(callback_uri: str) -> PendingFlickrAuthorization:
    """Fetch a temporary request token and build the user-facing authorize URL.

    Args:
        callback_uri: Absolute URL Flickr should redirect back to.

    Returns:
        The authorize URL to redirect the user to, plus the temporary
        request-token pair the caller must stash until the callback.

    Raises:
        FlickrNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the request-token step fails.
    """
    api_key, api_secret = _consumer_credentials()
    session = OAuth1Session(api_key, client_secret=api_secret, callback_uri=callback_uri)
    try:
        token = session.fetch_request_token(REQUEST_TOKEN_URL, timeout=_OAUTH_TIMEOUT)
    except Exception as exc:  # requests_oauthlib raises various requests/oauthlib errors
        raise GatewayRequestError(f"Flickr authorization could not be started: {exc}") from exc

    oauth_token = token["oauth_token"]
    oauth_token_secret = token["oauth_token_secret"]
    authorize_session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token, resource_owner_secret=oauth_token_secret)
    authorization_url = authorize_session.authorization_url(AUTHORIZE_URL, perms="read")
    return PendingFlickrAuthorization(authorization_url=authorization_url, oauth_token=oauth_token, oauth_token_secret=oauth_token_secret)


def finish_authorization(*, oauth_token: str, oauth_token_secret: str, oauth_verifier: str) -> FlickrAccessGrant:
    """Exchange a verified temporary token for the permanent access token pair.

    Args:
        oauth_token: The temporary request token from step 1 (echoed back by
            Flickr's callback - the caller should verify it matches).
        oauth_token_secret: The temporary token's secret, as stashed after step 1.
        oauth_verifier: The verifier Flickr's callback supplied.

    Returns:
        The permanent access token pair plus the connected Flickr user's id.

    Raises:
        FlickrNotConfiguredError: When the OAuth client is not configured.
        GatewayRequestError: When the access-token exchange fails.
    """
    api_key, api_secret = _consumer_credentials()
    session = OAuth1Session(
        api_key,
        client_secret=api_secret,
        resource_owner_key=oauth_token,
        resource_owner_secret=oauth_token_secret,
        verifier=oauth_verifier,
    )
    try:
        token = session.fetch_access_token(ACCESS_TOKEN_URL, timeout=_OAUTH_TIMEOUT)
    except Exception as exc:
        raise GatewayRequestError(f"Flickr authorization could not be completed: {exc}") from exc

    return FlickrAccessGrant(
        oauth_token=token["oauth_token"],
        oauth_token_secret=token["oauth_token_secret"],
        user_nsid=token["user_nsid"],
        username=token.get("username"),
    )
