"""Provider-agnostic Google OAuth 2.0 authorization-code flow helpers."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

from urbanlens.dashboard.services.core.gateway import UPSTREAM_BUSY_DEFAULT_SECONDS, GatewayRequestError, UpstreamBusyError, upstream_retry_after

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 # nosec B105 - OAuth endpoint URL, not a...
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

_OAUTH_TIMEOUT = 30


class GoogleOAuthNotConfiguredError(RuntimeError):
    """Raised when the site has no Google OAuth client configured."""


#: Token endpoint ``error`` codes that mean the user's grant itself is gone. RFC 6749 section 5.2 defines
#: ``invalid_grant`` as a refresh token that is "invalid, expired, revoked, ... or was issued to another client"; Google's
#: "Using OAuth 2.0 to Access Google APIs" (Refresh token expiration) answers a revoked or expired refresh token with
#: ``invalid_grant``, and a scope the user's Workspace admin restricted with ``admin_policy_enforced``.
DEAD_GRANT_ERRORS = frozenset({"invalid_grant", "admin_policy_enforced"})


class GoogleAuthExpiredError(GatewayRequestError):
    """Raised when Google has rejected the stored grant entirely (not a transient failure).
    Distinct from the generic ``GatewayRequestError`` so callers can tell "this connection is dead, prompt the user to reconnect" apart from a transient or unrelated API failure that doesn't warrant discarding the stored credentials."""


class GoogleTokenServiceBusyError(UpstreamBusyError):
    """Google's token endpoint did not refresh a token this time: a 5xx, a 408 or 429, an unusable 200, or no answer.

    Nothing about the grant was learned, so it stands.
    """


class GoogleOAuthClientRefusedError(GatewayRequestError):
    """The token endpoint refused the site's OAuth client or its request, not the user's grant.

    RFC 6749 section 5.2's ``invalid_client``, ``unauthorized_client``, ``invalid_request``, ``unsupported_grant_type``
    and ``invalid_scope``, and any other refusal Google does not name as the grant's: the operator's to fix, and every
    user's refresh fails the same way until they do.
    """


def _token_error(response: requests.Response) -> str:
    """The ``error`` code of a token endpoint refusal, or ``""`` when the body names none."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, str) else ""


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    *,
    access_type: str = "offline",
    prompt: str = "consent",
) -> str:
    """Build a Google consent-screen URL for an authorization-code flow.

    Args:
        client_id: The site's Google OAuth client id.
        redirect_uri: Absolute callback URL registered with the OAuth client.
        scopes: OAuth scopes to request.
        state: Signed opaque state token, verified on callback.
        access_type: ``"offline"`` (default) so Google issues a refresh token.
        prompt: ``"consent"`` (default) so a refresh token is issued even on a re-authorization.

    Returns:
        Fully-formed authorization URL to redirect the user to."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": access_type,
        "prompt": prompt,
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for access/refresh tokens.

    Args:
        client_id: The site's Google OAuth client id.
        client_secret: The site's Google OAuth client secret.
        code: Authorization code from the OAuth callback.
        redirect_uri: The same redirect URI used to obtain the code.

    Returns:
        Token response payload (``access_token``, ``refresh_token``, ``expires_in``, ``id_token``, ``scope``, ...).

    Raises:
        GatewayRequestError: When the token exchange fails."""
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=_OAUTH_TIMEOUT,
    )
    if response.status_code != 200:
        logger.error("Google token exchange failed (%s): %s", response.status_code, response.text[:500])
        raise GatewayRequestError("Google authorization failed.")
    return response.json()


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    """Obtain a fresh access token using a refresh token.

    Args:
        client_id: The site's Google OAuth client id.
        client_secret: The site's Google OAuth client secret.
        refresh_token: The stored OAuth refresh token.

    Returns:
        Token response payload (``access_token``, ``expires_in``, ...).

    Raises:
        GoogleAuthExpiredError: Google refused the grant itself (:data:`DEAD_GRANT_ERRORS`).
        GoogleTokenServiceBusyError: Google did not refresh it this time, for a reason that passes.
        GoogleOAuthClientRefusedError: Google refused the site's OAuth client or request; logged at ERROR."""
    try:
        response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=_OAUTH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Google token refresh got no response: %s", type(exc).__name__)
        raise GoogleTokenServiceBusyError("Google's token service did not answer.") from exc
    status = response.status_code
    if status >= 500 or status in (408, 429):
        logger.warning("Google token refresh failed (%s), for now", status)
        raise GoogleTokenServiceBusyError("Google's token service is unavailable.", retry_after=upstream_retry_after(response) or UPSTREAM_BUSY_DEFAULT_SECONDS)
    if status == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("access_token"), str) and payload["access_token"]:
            return payload
        logger.warning("Google token refresh answered 200 with no access token")
        raise GoogleTokenServiceBusyError("Google's token service sent no token.")
    error = _token_error(response)
    if error in DEAD_GRANT_ERRORS:
        logger.warning("Google refused a refresh token (%s, %s)", status, error)
        raise GoogleAuthExpiredError("Google access has expired or been revoked. Please reconnect.")
    # The body is Google's error, which holds no token; logged so the operator can see which setting to fix.
    logger.error("Google refused this site's OAuth client refreshing a token (%s, %s): %s", status, error or "no error code", response.text[:500])
    raise GoogleOAuthClientRefusedError("Google refused this site's OAuth client.")


def revoke_token(token: str) -> bool:
    """Best-effort revocation of an access or refresh token at Google.

    Args:
        token: The token to revoke (refresh token revokes the whole grant).

    Returns:
        True when Google confirmed the revocation.
    """
    try:
        response = requests.post(GOOGLE_REVOKE_URL, data={"token": token}, timeout=_OAUTH_TIMEOUT)
    except requests.RequestException:
        logger.warning("Google token revocation request failed", exc_info=True)
        return False
    return response.status_code == 200


def extract_email_from_id_token(id_token: str | None) -> str | None:
    """Read the ``email`` claim from an OAuth ``id_token``.
    The token arrives directly from Google's token endpoint over TLS, so the payload is decoded without signature verification - it is used for display only, never for authentication.

    Args:
        id_token: Raw JWT string from the token response, if any.

    Returns:
        The email claim, or None when absent or unparsable."""
    if not id_token:
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return None
    email = claims.get("email")
    return email if isinstance(email, str) else None
