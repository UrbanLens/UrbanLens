"""Channels WebSocket middleware accepting PAT/OAuth2 credentials, not just sessions.

``channels.auth.AuthMiddlewareStack`` only ever populates ``scope["user"]`` from Django's session
cookie, so a native client authenticating the way ``external_api`` does over HTTP - a PAT-style
``ApiKey`` bearer token or a django-oauth-toolkit OAuth2 access token - has no way to open
``ws/notifications/``, ``ws/messages/``, or the owner-side safety check-in chat
(``ws/safety/checkin/<uuid>/chat/``); all three gate on ``scope["user"].is_authenticated`` alone.
Over HTTP, resolving a credential is immediately followed by
``external_api.permissions.HasApiKeyScope``, which holds the credential to the scopes it was
actually granted; a socket that only learned *who* the credential belongs to would let any valid
bearer token reach every consumer, turning a narrow ``pins:read`` key into a pass for someone's
safety-check-in chat and letting a PAT reach direct messages that ``OAUTH2_ONLY_SCOPES`` refuses it
on every HTTP route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async

from urbanlens.dashboard.services.auth.api_keys import KEY_LABEL, api_key_candidate, finish_api_key_authentication, verify_api_key_secret

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

#: Scope key carrying the resolved ``ApiKey``/``AccessToken``, or None for a session/anonymous connection.
CREDENTIAL_SCOPE_KEY = "api_credential"


class ApiKeyAuthMiddleware:
    """ASGI middleware: fall back to a ``?key=`` query param when the session is anonymous."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        """Resolve a query-string credential, publishing both the user and the credential itself.

        Args:
            scope: The incoming ASGI connection scope.
            receive: ASGI receive callable, passed through untouched.
            send: ASGI send callable, passed through untouched.

        Returns:
            Whatever the wrapped application returns.
        """
        # Set unconditionally, and *first*, so the key exists even on the paths that resolve nothing.
        scope = {**scope, CREDENTIAL_SCOPE_KEY: None}
        user = scope.get("user")
        if user is None or not user.is_authenticated:
            token = self._extract_token(scope)
            if token:
                resolved = await self._resolve(token)
                if resolved is not None:
                    resolved_user, credential = resolved
                    scope = {**scope, "user": resolved_user, CREDENTIAL_SCOPE_KEY: credential}
        return await self.inner(scope, receive, send)

    @staticmethod
    def _extract_token(scope) -> str | None:
        """The ``key`` query-string parameter, if the connection URL carried one."""
        query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        values = parse_qs(query_string).get("key")
        return values[0] if values else None

    async def _resolve(self, token: str) -> tuple[AbstractBaseUser, Any] | None:
        """Resolve *token* as either a PAT key or an OAuth2 access token.

        Args:
            token: The raw ``?key=`` value presented by the client.

        Returns:
            ``(user, credential)`` on success - the credential being the same object DRF would put in
            ``request.auth`` for the equivalent HTTP...
        """
        if token.startswith(f"{KEY_LABEL}_"):
            return await self._resolve_api_key(token)
        return await database_sync_to_async(self._resolve_oauth2_token)(token)

    async def _resolve_api_key(self, token: str) -> tuple[AbstractBaseUser, Any] | None:
        """Resolve a PAT key, keeping the verification off the shared thread.

        ``database_sync_to_async`` is thread-sensitive by default, so every call to it in this process
        runs in the one executor thread all of Channels' database work queues on. A current-format key
        verifies in one SHA-256, but a key issued before P146 still costs a full PBKDF2 until its first
        use rewrites it, and one client reconnecting in a loop with such a key would stall every other
        socket's database access. So only the lookup and the writes run there.

        Args:
            token: The raw ``?key=`` value, already known to carry the PAT label.

        Returns:
            ``(user, key)`` when the secret checks out, else None.
        """
        candidate = await database_sync_to_async(api_key_candidate)(token)
        if candidate is None:
            return None
        api_key, secret = candidate
        is_correct, is_legacy = await sync_to_async(verify_api_key_secret, thread_sensitive=False)(secret, api_key.key_hash)
        if not is_correct:
            return None
        await database_sync_to_async(finish_api_key_authentication)(api_key, secret, is_legacy=is_legacy)
        return (api_key.user, api_key)

    @staticmethod
    def _resolve_oauth2_token(token: str) -> tuple[AbstractBaseUser, Any] | None:
        """Resolve a django-oauth-toolkit access token to its user and the token row.

        Args:
            token: The raw access-token string.

        Returns:
            ``(user, access_token)``, or None for an unknown or expired token, or for a client-credentials
            token that has no resource owner at all...
        """
        from oauth2_provider.models import get_access_token_model

        access_token_model = get_access_token_model()
        access_token = access_token_model.objects.select_related("user").filter(token=token).first()
        if access_token is None or access_token.is_expired() or access_token.user is None:
            return None
        return (access_token.user, access_token)


def ApiKeyAuthMiddlewareStack(inner):  # noqa: N802 - mirrors channels.auth.AuthMiddlewareStack's own naming
    """``AuthMiddlewareStack`` plus the PAT/OAuth2 fallback, in the right nesting order."""
    return AuthMiddlewareStack(ApiKeyAuthMiddleware(inner))
