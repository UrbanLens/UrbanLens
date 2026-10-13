"""Tests for ApiKeyAuthMiddleware - PAT/OAuth2 fallback auth for Channels sockets."""

from __future__ import annotations

from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.core.tests.testcase import TransactionTestCase
from urbanlens.dashboard.consumers import UserNotificationConsumer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

Application = get_application_model()
AccessToken = get_access_token_model()


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _notification_key(user) -> tuple[ApiKey, str]:
    """
    Issue a key scoped for ``ws/notifications/`` - see this module's docstring.

    Args:
        user: The account the key belongs to.

    Returns:
        Tuple of (the ``ApiKey`` row, its one-time plaintext)."""
    api_key, raw_key = generate_api_key(user, "Mobile app")
    ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.NOTIFICATIONS_READ.value])
    return api_key, raw_key


def _bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


class ApiKeyWebSocketAuthTests(TransactionTestCase):
    """A bearer header (or the deprecated ``?key=`` param) authenticates a socket exactly like a session would."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)

    def _communicator(self, path: str, *, user=None, headers=None) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(ApiKeyAuthMiddleware(UserNotificationConsumer.as_asgi()), path, headers=headers)
        comm.scope["user"] = user if user is not None else AnonymousUser()
        return comm

    def test_valid_api_key_authenticates_an_anonymous_socket(self) -> None:
        _api_key, raw_key = _notification_key(self.user)

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={raw_key}")
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_no_key_and_no_session_is_rejected(self) -> None:
        async def _test():
            comm = self._communicator("/ws/notifications/")
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_malformed_key_is_rejected(self) -> None:
        async def _test():
            comm = self._communicator("/ws/notifications/?key=garbage")
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_revoked_key_is_rejected(self) -> None:
        api_key, raw_key = _notification_key(self.user)
        ApiKey.objects.filter(pk=api_key.pk).update(revoked_at=api_key.created)

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={raw_key}")
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_existing_session_user_wins_over_a_key_param(self) -> None:
        """A key belonging to a *different* user must never override an authenticated session."""
        from channels.layers import get_channel_layer

        from urbanlens.dashboard.models.notifications.signals import notification_group_name

        other = baker.make(User)
        _api_key, raw_key = generate_api_key(other, "Mobile app")

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={raw_key}", user=self.user)
            connected, _ = await comm.connect()
            self.assertTrue(connected)

            channel_layer = get_channel_layer()
            # Joined self.user's group (the session), not other's (the key) -
            # a payload for other's profile must never arrive on this socket.
            await channel_layer.group_send(
                notification_group_name(other.profile.pk),
                {"type": "notification.new", "notification": {"id": "should-not-arrive"}},
            )
            await channel_layer.group_send(
                notification_group_name(self.user.profile.pk),
                {"type": "notification.new", "notification": {"id": "should-arrive"}},
            )
            received = await comm.receive_from()
            self.assertIn("should-arrive", received)
            self.assertTrue(await comm.receive_nothing())

            await comm.disconnect()

        _run(_test())

    def test_oauth2_access_token_authenticates_an_anonymous_socket(self) -> None:
        application = Application.objects.create(
            name="UrbanLens Mobile",
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="urbanlens://oauth/callback",
        )
        token = AccessToken.objects.create(
            user=self.user,
            application=application,
            token="tok-notifications",
            expires=timezone.now() + timedelta(hours=1),
            scope="notifications:read",
        )

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={token.token}")
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_expired_oauth2_access_token_is_rejected(self) -> None:
        application = Application.objects.create(
            name="UrbanLens Mobile",
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="urbanlens://oauth/callback",
        )
        token = AccessToken.objects.create(
            user=self.user,
            application=application,
            token="tok-expired",
            expires=timezone.now() - timedelta(hours=1),
            scope="profile:read",
        )

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={token.token}")
            connected, close_code = await comm.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

        _run(_test())

    def test_bearer_header_authenticates_with_a_pat(self) -> None:
        _api_key, raw_key = _notification_key(self.user)

        async def _test():
            comm = self._communicator("/ws/notifications/", headers=_bearer(raw_key))
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_bearer_header_authenticates_with_an_oauth2_token(self) -> None:
        application = Application.objects.create(
            name="UrbanLens Mobile",
            user=self.user,
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="urbanlens://oauth/callback",
        )
        token = AccessToken.objects.create(
            user=self.user,
            application=application,
            token="tok-header",
            expires=timezone.now() + timedelta(hours=1),
            scope="notifications:read",
        )

        async def _test():
            comm = self._communicator("/ws/notifications/", headers=_bearer(token.token))
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_bearer_header_wins_over_key_param(self) -> None:
        """A valid header and a different user's ?key=: the socket belongs to the header's user."""
        from channels.layers import get_channel_layer

        from urbanlens.dashboard.models.notifications.signals import notification_group_name

        other = baker.make(User)
        _api_key, header_key = _notification_key(self.user)
        _other_key, query_key = _notification_key(other)

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={query_key}", headers=_bearer(header_key))
            connected, _ = await comm.connect()
            self.assertTrue(connected)

            channel_layer = get_channel_layer()
            await channel_layer.group_send(
                notification_group_name(other.profile.pk),
                {"type": "notification.new", "notification": {"id": "should-not-arrive"}},
            )
            await channel_layer.group_send(
                notification_group_name(self.user.profile.pk),
                {"type": "notification.new", "notification": {"id": "should-arrive"}},
            )
            received = await comm.receive_from()
            self.assertIn("should-arrive", received)
            self.assertTrue(await comm.receive_nothing())

            await comm.disconnect()

        _run(_test())

    def test_key_param_still_works_without_a_header(self) -> None:
        _api_key, raw_key = _notification_key(self.user)

        async def _test():
            comm = self._communicator(f"/ws/notifications/?key={raw_key}", headers=[(b"user-agent", b"okhttp")])
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.disconnect()

        _run(_test())

    def test_non_bearer_authorization_header_is_ignored(self) -> None:
        """A Basic header does not authenticate, and does not stop ?key= from doing so."""
        _api_key, raw_key = _notification_key(self.user)

        async def _test():
            rejected = self._communicator(
                "/ws/notifications/", headers=[(b"authorization", f"Basic {raw_key}".encode())]
            )
            connected, close_code = await rejected.connect()
            self.assertFalse(connected)
            self.assertEqual(close_code, 4404)

            fallback = self._communicator(
                f"/ws/notifications/?key={raw_key}", headers=[(b"authorization", b"Basic dXNlcjpwdw==")]
            )
            connected, _ = await fallback.connect()
            self.assertTrue(connected)
            await fallback.disconnect()

        _run(_test())

    def test_extract_token_prefers_header_and_ignores_malformed_ones(self) -> None:
        extract = ApiKeyAuthMiddleware._extract_token
        self.assertEqual(extract({"headers": [(b"Authorization", b"bearer  abc ")], "query_string": b"key=q"}), "abc")
        self.assertEqual(extract({"headers": [(b"authorization", b"Bearer")], "query_string": b"key=q"}), "q")
        self.assertEqual(extract({"headers": [(b"authorization", b"Bearer\tabc")], "query_string": b"key=q"}), "abc")
        self.assertEqual(extract({"headers": [(b"authorization", b"Bearer   ")], "query_string": b"key=q"}), "q")
        self.assertEqual(extract({"headers": [(b"authorization", b"Token abc")], "query_string": b""}), None)
        self.assertEqual(extract({"headers": [], "query_string": b"key=q"}), "q")
        self.assertIsNone(extract({"query_string": b""}))
