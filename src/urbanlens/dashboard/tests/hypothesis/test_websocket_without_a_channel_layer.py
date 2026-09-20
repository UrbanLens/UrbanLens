"""What a socket does on a deployment that has no channel layer at all.

``settings/base.py`` only defines ``CHANNEL_LAYERS`` when a Dragonfly/Redis URL is configured, so an
install without one - a self-hoster following the smallest path, or an environment whose
``UL_DRAGONFLY_URL`` never reached the container - has no layer. Channels only sets
``channel_name`` on a consumer when a layer exists, so every consumer here reaches for an attribute
that is not there. The deployment cannot deliver live notifications either way; the difference is
whether the browser is told so or is answered with a 500 per attempt, and whether the log says why.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.dashboard.consumers import UserNotificationConsumer

#: What the consumer closes with when this deployment cannot carry a socket at all.
NO_CHANNEL_LAYER = 4503


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


@override_settings(CHANNEL_LAYERS={})
class SocketsWithoutAChannelLayerTests(TransactionTestCase):
    """A deployment with nowhere to send a broadcast refuses the socket rather than erroring on it."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)

    def test_the_setting_is_really_gone(self) -> None:
        """Guards the two tests below: they prove nothing if a layer is still configured."""
        from channels.layers import get_channel_layer

        self.assertIsNone(
            get_channel_layer(), "a channel layer is still configured, so these tests are measuring the normal path"
        )

    def _attempt(self) -> tuple[bool, int | None]:
        """Try one handshake, start to finish.

        Connect and disconnect share one event loop: each ``async_to_sync`` call makes its own, and
        cancelling an application from a loop that did not start it raises rather than tears down.

        Returns:
            Whether the socket was accepted, and the close code when it was not.
        """

        async def attempt() -> tuple[bool, int | None]:
            communicator = WebsocketCommunicator(UserNotificationConsumer.as_asgi(), "/ws/notifications/")
            communicator.scope["user"] = self.user
            connected, code = await communicator.connect()
            await communicator.disconnect()
            return connected, code

        return _run(attempt())

    def test_a_socket_is_refused_rather_than_raising(self) -> None:
        """The handshake is answered - with a close - instead of raising `AttributeError` into Channels."""
        connected, code = self._attempt()

        self.assertFalse(
            connected, "a deployment with no channel layer accepted a socket it can never deliver anything on"
        )
        self.assertEqual(code, NO_CHANNEL_LAYER)

    def test_the_account_is_not_charged_for_a_socket_it_never_got(self) -> None:
        """A refused connection must not leave a slot claimed - the allowance is per account and small."""
        from urbanlens.dashboard.services.security import socket_budget

        self._attempt()

        self.assertEqual(socket_budget.open_count(f"user:{self.user.pk}"), 0)
