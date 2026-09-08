"""One sender's message budget, whichever door the message comes through.

Every write the chat sockets perform is also reachable over plain HTTP. The
socket budget landed first (P31), and on its own it is theatre: the same
``create_direct_message`` / ``create_group_message`` / ``create_chat_message``
calls sit behind ``ConversationSendView``, ``GroupSendView`` and
``SafetyCheckinMessageView``, which are plain Django ``View``s that DRF's
throttle classes do not cover. A POST loop routes straight around a limit the
socket enforces.

So the budget lives in the service layer, where both doors meet, and these tests
assert that from both sides:

- the HTTP path alone is bounded, and
- a budget already spent over the socket is *still* spent when the sender
  switches to HTTP.

The second is the one that matters. A per-door limit that happens to exist on
both doors is not the same thing as one budget, and only the cross-door test can
tell them apart.

**There are five doors, not two.** The first version of this file covered the
socket and the two web views and stopped there, which is what let the external
API - three more views calling the identical create functions - answer a
throttled send with a 500. ``MessageRateLimitedError`` is a bare ``ValueError``,
and DRF's exception handler returns ``None`` for anything that is not an
``APIException``, which Django then renders as a server error. The mapping lives
in ``uniform_exception_handler`` rather than in each view, so a view added later
inherits it.
"""

from __future__ import annotations

import json
import os

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import DirectMessageConsumer
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class DirectMessageHttpBudgetTests(TransactionTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.sender = _make_profile()
        self.recipient = _make_profile()
        friendship = Friendship.request(self.sender, self.recipient)
        assert friendship is not None
        friendship.accept()
        self.client.force_login(self.sender.user)
        self.url = reverse("messages.send", kwargs={"profile_slug": self.recipient.slug})

    def _saved(self) -> int:
        return DirectMessage.objects.filter(sender=self.sender).count()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_the_http_fallback_is_bounded(self) -> None:
        """A POST loop must not route around the socket's limit."""
        statuses = [self.client.post(self.url, {"body": f"message {index}"}).status_code for index in range(8)]

        self.assertEqual(self._saved(), 3, "the HTTP send path is unbudgeted")
        self.assertIn(429, statuses, "a refused send did not say why")

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_a_refusal_names_the_reason(self) -> None:
        for index in range(4):
            response = self.client.post(self.url, {"body": f"message {index}"})

        self.assertEqual(response.status_code, 429)
        self.assertIn(b"too quickly", response.content)

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_a_budget_spent_on_the_socket_is_spent_on_http_too(self) -> None:
        """One budget, not two. Two per-door limits would both pass the test above
        and still let a sender send twice their allowance by alternating doors."""
        _run(self._spend_on_the_socket())

        response = self.client.post(self.url, {"body": "over http"})

        self.assertEqual(self._saved(), 2, "the socket and HTTP paths hold separate budgets")
        self.assertEqual(response.status_code, 429)

    async def _spend_on_the_socket(self) -> None:
        comm = WebsocketCommunicator(DirectMessageConsumer.as_asgi(), "/ws/messages/")
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = self.sender.user
        connected, _ = await comm.connect()
        assert connected
        for index in range(2):
            await comm.send_to(text_data=json.dumps({"recipient": self.recipient.slug, "body": f"socket {index}"}))
        # The socket answers nothing on the sending connection, so wait for the
        # rows rather than for a frame - see test_websocket_volume_dm_and_games.
        from channels.db import database_sync_to_async

        acount = database_sync_to_async(self._saved)
        for _ in range(25):
            if await acount() >= 2:
                break
            await comm.receive_nothing(timeout=0.2)
        await comm.disconnect()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_a_rejected_message_does_not_spend_the_budget(self) -> None:
        """Validation refusals are the sender's client being wrong, not the sender
        sending too much - charging them would throttle someone for a bug."""
        for _ in range(5):
            self.client.post(self.url, {"body": "   "})

        for index in range(3):
            self.client.post(self.url, {"body": f"real {index}"})

        self.assertEqual(self._saved(), 3, "refused messages spent the budget")


class GroupMessageHttpBudgetTests(TransactionTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        from urbanlens.dashboard.services.messaging.group_chats import create_group_chat

        self.sender = _make_profile()
        self.other = _make_profile()
        friendship = Friendship.request(self.sender, self.other)
        assert friendship is not None
        friendship.accept()
        self.group = create_group_chat(self.sender, "Test group", [self.other])
        self.client.force_login(self.sender.user)
        self.url = reverse("messages.group.send", kwargs={"group_uuid": self.group.uuid})

    def _saved(self) -> int:
        from urbanlens.dashboard.models.group_chats.model import GroupMessage

        return GroupMessage.objects.filter(sender=self.sender).count()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_the_http_fallback_is_bounded(self) -> None:
        statuses = [self.client.post(self.url, {"body": f"message {index}"}).status_code for index in range(8)]

        self.assertEqual(self._saved(), 3, "the group HTTP send path is unbudgeted")
        self.assertIn(429, statuses)

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_group_and_direct_messages_share_one_sender_budget(self) -> None:
        """One person's outbound messaging rate, not one per conversation shape.

        Keeping them separate would let a sender double their allowance by
        alternating between a group and a DM.
        """
        recipient = _make_profile()
        friendship = Friendship.request(self.sender, recipient)
        assert friendship is not None
        friendship.accept()

        self.client.post(self.url, {"body": "group one"})
        self.client.post(self.url, {"body": "group two"})
        response = self.client.post(reverse("messages.send", kwargs={"profile_slug": recipient.slug}), {"body": "dm"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(DirectMessage.objects.filter(sender=self.sender).count(), 0)


class ExternalApiBudgetTests(TestCase):
    """The three API doors that call the same create functions the web views do."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        baker.make(User)  # first user is auto-promoted to site admin
        self.sender = _make_profile()
        self.partner = _make_profile()
        friendship = Friendship.request(self.sender, self.partner)
        assert friendship is not None
        friendship.accept()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self._token()}"}

    def _token(self) -> str:
        from datetime import timedelta

        from django.utils import timezone
        from oauth2_provider.models import get_access_token_model

        from urbanlens.core.tests.oauth import first_party_application
        from urbanlens.dashboard.models.account.model import ApiKeyScope

        token = get_access_token_model().objects.create(
            user=self.sender.user,
            application=first_party_application(),
            token=f"tok-budget-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}",
        )
        return token.token

    def _send(self, body: str):
        url = reverse("external_api:messages.thread", kwargs={"peer_slug": self.partner.ensure_slug()})
        return self.client.post(url, data=json.dumps({"body": body}), content_type="application/json", **self.auth)

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_a_throttled_send_is_a_429_not_a_500(self) -> None:
        """A 500 tells an API client to retry a request the server thinks is broken,
        fires error alerting for benign traffic, and hides the actual reason."""
        statuses = [self._send(f"message {index}").status_code for index in range(5)]

        self.assertNotIn(500, statuses, "a throttled send raised through DRF as a server error")
        self.assertIn(429, statuses, "the throttle never refused an API send")

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_the_refusal_carries_the_reason(self) -> None:
        for index in range(3):
            response = self._send(f"message {index}")

        self.assertEqual(response.status_code, 429)
        self.assertIn("too quickly", response.json()["error"])

    @override_settings(UL_MESSAGES_PER_MINUTE=2)
    def test_the_api_shares_the_budget_with_the_web_view(self) -> None:
        """Five doors, one budget. Per-door limits would pass every test above."""
        self.client.force_login(self.sender.user)
        self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}),
            {"body": "over the web"},
        )
        self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}),
            {"body": "over the web again"},
        )

        self.assertEqual(self._send("over the api").status_code, 429)


class ChargePlacementTests(TestCase):
    """A charge is for a message that would otherwise have been delivered."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        baker.make(User)
        self.sender = _make_profile()

    @override_settings(UL_MESSAGES_PER_MINUTE=3)
    def test_a_recipient_who_refuses_messages_does_not_cost_the_sender_their_budget(self) -> None:
        """The default ``direct_message_visibility`` is ANYTHING_IN_COMMON, so
        messaging someone new and being refused is ordinary use of a site built
        around discovering other people - not abuse to be charged for.

        ``create_group_message`` already checks membership before charging; this
        is the same precondition on the other function.
        """
        from urbanlens.dashboard.services.core.message_limits import MessageRateLimitedError
        from urbanlens.dashboard.services.messaging.direct_messages import (
            RecipientNotAcceptingMessagesError,
            create_direct_message,
        )

        stranger = _make_profile()
        for _ in range(5):
            with self.assertRaises(RecipientNotAcceptingMessagesError):
                create_direct_message(self.sender, stranger, "let me in")

        friend = _make_profile()
        friendship = Friendship.request(self.sender, friend)
        assert friendship is not None
        friendship.accept()
        for index in range(3):
            create_direct_message(self.sender, friend, f"real {index}")

        with self.assertRaises(MessageRateLimitedError):
            create_direct_message(self.sender, friend, "one too many")

        self.assertEqual(DirectMessage.objects.filter(sender=self.sender).count(), 3)
