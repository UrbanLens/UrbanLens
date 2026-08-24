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
resolves an Authorization bearer credential (or legacy ``?key=<token>`` query
parameter) using the exact same lookups
``external_api.authentication.ApiKeyAuthentication``/``OAuth2Authentication``
use over HTTP. A session, when present, always wins.

Authenticating is only half the job, though. Over HTTP, resolving a credential
is immediately followed by ``external_api.permissions.HasApiKeyScope``, which
holds the credential to the scopes it was actually granted; a socket that only
learned *who* the credential belongs to would let any valid bearer token reach
every consumer, turning a narrow ``pins:read`` key into a pass for someone's
safety-check-in chat and letting a PAT reach direct messages that
``OAUTH2_ONLY_SCOPES`` refuses it on every HTTP route. So this middleware also
publishes the resolved credential itself as ``scope["api_credential"]``, and
the consumers run the same ``credential_grants`` check DRF does.

``scope["api_credential"]`` is always present and is ``None`` for a
session-authenticated (or anonymous) connection. That None is load-bearing: it
is exactly the discriminator ``external_api.mixins.IsSessionAuthenticated``
uses over HTTP (``request.auth is None`` means "a browser session, not a
credential"), and it is what lets the consumers apply scope enforcement to
credential connections while leaving the web client's behavior byte-for-byte
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async

from urbanlens.dashboard.services.api_keys import KEY_LABEL, authenticate_api_key

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

#: Scope key carrying the resolved ``ApiKey``/``AccessToken``, or None for a
#: session/anonymous connection. Consumers read it through
#: ``scope.get(...)`` rather than ``scope[...]``: unit tests (and any future
#: ASGI entrypoint) may instantiate a consumer without this middleware in the
#: stack, and a missing key must degrade to "no credential", never to a
#: KeyError that kills the socket.
CREDENTIAL_SCOPE_KEY = "api_credential"


class ApiKeyAuthMiddleware:
    """ASGI middleware: fall back to a bearer token when the session is anonymous."""

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
        # Set unconditionally, and *first*, so the key exists even on the paths
        # that resolve nothing. A consumer must be able to tell "no credential"
        # from "credential not looked at yet"; leaving the key absent on some
        # branches would make the two indistinguishable.
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
        """The bearer credential from headers, or the legacy ``key`` query parameter."""
        for name, value in scope.get("headers", ()):
            if name.lower() != b"authorization":
                continue
            header = value.decode("utf-8", errors="ignore").strip()
            scheme, _, token = header.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
        query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        values = parse_qs(query_string).get("key")
        return values[0] if values else None

    @database_sync_to_async
    def _resolve(self, token: str) -> tuple[AbstractBaseUser, Any] | None:
        """Resolve *token* as either a PAT key or an OAuth2 access token.

        Args:
            token: The raw ``?key=`` value presented by the client.

        Returns:
            ``(user, credential)`` on success - the credential being the same
            object DRF would put in ``request.auth`` for the equivalent HTTP
            request, so ``external_api.permissions.credential_grants`` can be
            applied to it verbatim. None when the token doesn't resolve.
        """
        if token.startswith(f"{KEY_LABEL}_"):
            api_key = authenticate_api_key(token)
            return (api_key.user, api_key) if api_key is not None else None
        return self._resolve_oauth2_token(token)

    @staticmethod
    def _resolve_oauth2_token(token: str) -> tuple[AbstractBaseUser, Any] | None:
        """Resolve a django-oauth-toolkit access token to its user and the token row.

        Args:
            token: The raw access-token string.

        Returns:
            ``(user, access_token)``, or None for an unknown or expired token,
            or for a client-credentials token that has no resource owner at
            all (there is no user for such a token to act as here).
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
