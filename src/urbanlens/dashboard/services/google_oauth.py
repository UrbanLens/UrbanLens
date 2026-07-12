"""Provider-agnostic Google OAuth 2.0 authorization-code flow helpers.

Extracted from ``dashboard/services/apis/calendar/google.py`` so any feature
needing its own Google OAuth grant (Calendar, Google Photos, ...) can reuse
the same token exchange/refresh/revoke mechanics against the site's one
Google OAuth client (``UL_GOOGLE_CLIENT_ID``/``UL_GOOGLE_CLIENT_SECRET``),
each requesting whatever scopes its feature needs. Every function here is
scope-agnostic - callers pass their own ``scopes``/client credentials rather
than this module hardcoding any one feature's grant.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

from urbanlens.dashboard.services.gateway import GatewayRequestError

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 # nosec B105 - OAuth endpoint URL, not a credential
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

_OAUTH_TIMEOUT = 30


class GoogleOAuthNotConfiguredError(RuntimeError):
    """Raised when the site has no Google OAuth client configured."""


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
        prompt: ``"consent"`` (default) so a refresh token is issued even on
            a re-authorization.

    Returns:
        Fully-formed authorization URL to redirect the user to.
    """
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
        Token response payload (``access_token``, ``refresh_token``,
        ``expires_in``, ``id_token``, ``scope``, ...).

    Raises:
        GatewayRequestError: When the token exchange fails.
    """
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
        GatewayRequestError: When the refresh fails (e.g. access revoked).
    """
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
    if response.status_code != 200:
        logger.warning("Google token refresh failed (%s): %s", response.status_code, response.text[:500])
        raise GatewayRequestError("Google access has expired or been revoked. Please reconnect.")
    return response.json()


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

    The token arrives directly from Google's token endpoint over TLS, so the
    payload is decoded without signature verification - it is used for
    display only, never for authentication.

    Args:
        id_token: Raw JWT string from the token response, if any.

    Returns:
        The email claim, or None when absent or unparsable.
    """
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
