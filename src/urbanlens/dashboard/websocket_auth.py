"""Channels WebSocket middleware accepting PAT/OAuth2 credentials, not just sessions.

``channels.auth.AuthMiddlewareStack`` only ever populates ``scope["user"]``
from Django's session cookie, so a native client authenticating the way
``external_api`` does over HTTP - a PAT-style ``ApiKey`` bearer token or a
django-oauth-toolkit OAuth2 access token - has no way to open
``ws/notifications/``, ``ws/messages/``, or the owner-side safety check-in
chat (``ws/safety/checkin/<uuid>/chat/``); all three gate on
``scope["user"].is_authenticated`` alone. The token-authenticated contact
route (``ws/safety/contact/<token>/chat/``) is unaffected - it resolves its
token itself, inside the consumer, independent of ``scope["user"]``.

:class:`ApiKeyAuthMiddleware` closes that gap without touching any consumer:
nested *inside* ``AuthMiddlewareStack`` (see :func:`ApiKeyAuthMiddlewareStack`),
it only runs once the session has already left the connection anonymous, and
resolves a ``?key=<token>`` query-string credential using the exact same
lookups ``external_api.authentication.ApiKeyAuthentication``/
``OAuth2Authentication`` use over HTTP. A session, when present, always wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async

from urbanlens.dashboard.services.api_keys import KEY_LABEL, authenticate_api_key

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


class ApiKeyAuthMiddleware:
    """ASGI middleware: fall back to a ``?key=`` query param when the session is anonymous."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        user = scope.get("user")
        if user is None or not user.is_authenticated:
            token = self._extract_token(scope)
            if token:
                resolved = await self._resolve(token)
                if resolved is not None:
                    scope = {**scope, "user": resolved}
        return await self.inner(scope, receive, send)

    @staticmethod
    def _extract_token(scope) -> str | None:
        """The ``key`` query-string parameter, if the connection URL carried one."""
        query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        values = parse_qs(query_string).get("key")
        return values[0] if values else None

    @database_sync_to_async
    def _resolve(self, token: str) -> AbstractBaseUser | None:
        """Resolve *token* as either a PAT key or an OAuth2 access token."""
        if token.startswith(f"{KEY_LABEL}_"):
            api_key = authenticate_api_key(token)
            return api_key.user if api_key is not None else None
        return self._resolve_oauth2_token(token)

    @staticmethod
    def _resolve_oauth2_token(token: str) -> AbstractBaseUser | None:
        from oauth2_provider.models import get_access_token_model

        access_token_model = get_access_token_model()
        access_token = access_token_model.objects.select_related("user").filter(token=token).first()
        if access_token is None or access_token.is_expired():
            return None
        return access_token.user


def ApiKeyAuthMiddlewareStack(inner):  # noqa: N802 - mirrors channels.auth.AuthMiddlewareStack's own naming
    """``AuthMiddlewareStack`` plus the PAT/OAuth2 fallback, in the right nesting order."""
    return AuthMiddlewareStack(ApiKeyAuthMiddleware(inner))
