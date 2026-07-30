"""API-key bearer-token authentication for the external API.

Wired into ``external_api`` views only - never added to
``REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`` in settings, so it has no
effect on the internal, session-authenticated REST surface under
``dashboard/rest/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from urbanlens.dashboard.services.api_keys import KEY_LABEL, authenticate_api_key, record_api_key_usage

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from rest_framework.request import Request

    from urbanlens.dashboard.models.account.model import ApiKey


class ApiKeyAuthentication(BaseAuthentication):
    """Authenticates a request bearing ``Authorization: Bearer <api-key>``.

    On success, DRF stashes the returned ``(user, auth)`` tuple as
    ``request.user``/``request.auth`` - ``external_api.permissions.HasApiKeyScope``
    reads ``request.auth`` (the ``ApiKey`` row) to check its granted scopes.
    """

    keyword = "Bearer"

    def authenticate(self, request: Request) -> tuple[User, ApiKey] | None:
        """Resolve the bearer token in ``Authorization``, if present.

        Args:
            request: The incoming DRF request.

        Returns:
            ``(user, api_key)`` on success; ``None`` when no bearer token was
            presented at all, or the token isn't ``ulk_``-labeled (letting
            other authenticators - notably OAuth2 access tokens, which share
            the ``Bearer`` scheme - or anonymous access take over).

        Raises:
            AuthenticationFailed: A ``ulk_``-labeled bearer token was
                presented but doesn't resolve to an active key.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith(f"{self.keyword} "):
            return None

        raw_key = auth_header[len(self.keyword) + 1 :].strip()
        if not raw_key.startswith(f"{KEY_LABEL}_"):
            # Not an API key at all - claim nothing, so a non-ulk bearer token
            # (an OAuth2 access token) isn't falsely rejected here before its
            # own authenticator gets a look.
            return None

        api_key = authenticate_api_key(raw_key)
        if api_key is None:
            raise AuthenticationFailed("Invalid or revoked API key.")

        # Logged here (once per successfully authenticated request) rather than
        # per-view, so every current and future external_api endpoint gets
        # activity tracking automatically. Never logged for a rejected key -
        # see record_api_key_usage's docstring for why.
        record_api_key_usage(api_key, request.path)

        return (api_key.user, api_key)

    def authenticate_header(self, request: Request) -> str:
        """The ``WWW-Authenticate`` challenge scheme for a 401 response."""
        return self.keyword
