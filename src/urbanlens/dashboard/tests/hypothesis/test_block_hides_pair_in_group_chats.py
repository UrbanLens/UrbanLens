"""Two people who blocked each other share a group chat without seeing each other in it (I7, rulings 1b and 2b).

Adding either to a group the other is in is allowed and refuses nothing. From the moment of the block, each
stops receiving what the other sends - thread, inbox preview, unread count, notification, live frame,
reaction attribution - and stops appearing in the other's member list. What was sent before the block stays.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import os
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import DirectMessageConsumer
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.e2ee import GroupKey
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.messaging.direct_messages import direct_message_group_name, reaction_summary
from urbanlens.dashboard.services.messaging.group_chats import (
    GroupChatPermissionError,
    add_group_members,
    create_group_chat,
    create_group_message,
    delete_group_message,
    group_conversations_for,
    group_thread_page,
    toggle_group_reaction,
    unread_group_conversation_count,
)
from urbanlens.dashboard.services.social.friendship import block_profile, unblock_profile
from urbanlens.dashboard.tasks import broadcast_channel_group_messages

AccessToken = get_access_token_model()
READ_WRITE = f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}"


def _profile(name: str) -> Profile:
    user = baker.make("auth.User", username=f"{name}{os.urandom(3).hex()}")
    # Everyone identifiable to everyone, so an absence below is the block's doing and not a privacy mask.
    Profile.objects.filter(user=user).update(
        direct_message_visibility=VisibilityChoice.ANYONE, profile_visibility=VisibilityChoice.ANYONE
    )
    profile = Profile.objects.select_related("user").get(user=user)
    profile.ensure_slug()
    return profile


def _blob(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _bearer(user) -> dict:
    token = AccessToken.objects.create(
        user=user,
        application=first_party_application(),
        token=f"tok-{os.urandom(8).hex()}",
        expires=timezone.now() + timedelta(hours=1),
        scope=READ_WRITE,
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}


def _age_group(group: GroupChat, *, minutes: int) -> None:
    """Move a group's creation and memberships into the past, so pre-block messages can be dated between."""
    then = timezone.now() - timedelta(minutes=minutes)
    GroupChat.objects.filter(pk=group.pk).update(created=then)
    GroupChatMembership.objects.filter(group=group).update(created=then)


def _age(message: GroupMessage, *, minutes: int) -> GroupMessage:
    GroupMessage.objects.filter(pk=message.pk).update(created=timezone.now() - timedelta(minutes=minutes))
    message.refresh_from_db()
    return message


def _bodies(messages) -> set[str]:
    return {message.body for message in messages}


class AddingIsAllowedTests(TestCase):
    """Ruling 1b: nothing about the add changes, so nothing tells the person adding that a block exists."""

    def setUp(self) -> None:
        super().setUp()
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.carol = _profile("carol")
        block_profile(self.alice, self.bob)

    def test_a_group_can_be_created_with_both(self) -> None:
        group = create_group_chat(self.carol, "Quarry crew", [self.alice, self.bob])

        self.assertEqual(
            set(group.active_memberships().values_list("profile_id", flat=True)),
            {self.alice.pk, self.bob.pk, self.carol.pk},
        )

    def test_the_blocked_person_can_be_added_later(self) -> None:
        group = create_group_chat(self.carol, "Quarry crew", [self.alice])

        created = add_group_members(group, self.carol, [self.bob])

        self.assertEqual([membership.profile_id for membership in created], [self.bob.pk])

    def test_the_web_add_answers_exactly_as_an_unblocked_add_does(self) -> None:
        group = create_group_chat(self.carol, "Quarry crew", [self.alice])
        stranger = _profile("dave")
        self.client.force_login(self.carol.user)
        url = reverse("messages.group.members.add", kwargs={"group_uuid": group.uuid})

        blocked_add = self.client.post(url, {"member_slugs": [self.bob.slug]})
        plain_add = self.client.post(url, {"member_slugs": [stranger.slug]})

        self.assertEqual(blocked_add.status_code, 200)
        self.assertEqual(plain_add.status_code, 200)
        self.assertIsNotNone(group.membership_for(self.bob))

    def test_the_external_api_add_answers_as_an_unblocked_add_does(self) -> None:
        group = create_group_chat(self.carol, "Quarry crew", [self.alice])

        response = self.client.post(
            reverse("external_api:messages.groups.members", kwargs={"group_uuid": group.uuid}),
            data=json.dumps({"member_slugs": [self.bob.slug]}),
            content_type="application/json",
            **_bearer(self.carol.user),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"added": 1})


class _BlockedPairInAGroup(TestCase):
    """Carol's group holds Alice and Bob. Bob speaks, then Alice blocks Bob, then everyone speaks again."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.carol = _profile("carol")
        self.group = create_group_chat(self.carol, "Quarry crew", [self.alice, self.bob])
        _age_group(self.group, minutes=60)
        self.bob_before = _age(create_group_message(self.bob, self.group, "bob before the block"), minutes=30)
        block_profile(self.alice, self.bob)
        self.bob_after = create_group_message(self.bob, self.group, "bob after the block")
        self.carol_after = create_group_message(self.carol, self.group, "carol after the block")

    def _membership(self, profile: Profile) -> GroupChatMembership:
        membership = self.group.membership_for(profile)
        assert membership is not None
        return membership


class ThreadTests(_BlockedPairInAGroup):
    def test_the_blocker_does_not_see_what_the_blocked_person_sent_afterwards(self) -> None:
        messages, _ = group_thread_page(self._membership(self.alice))

        self.assertNotIn("bob after the block", _bodies(messages))

    def test_what_was_sent_before_the_block_stays(self) -> None:
        messages, _ = group_thread_page(self._membership(self.alice))

        self.assertIn("bob before the block", _bodies(messages))

    def test_everyone_else_is_unaffected(self) -> None:
        alices, _ = group_thread_page(self._membership(self.alice))
        carols, _ = group_thread_page(self._membership(self.carol))

        self.assertIn("carol after the block", _bodies(alices))
        self.assertIn("bob after the block", _bodies(carols))

    def test_the_blocked_person_does_not_see_the_blocker_either(self) -> None:
        create_group_message(self.alice, self.group, "alice after the block")

        bobs, _ = group_thread_page(self._membership(self.bob))
        carols, _ = group_thread_page(self._membership(self.carol))

        self.assertNotIn("alice after the block", _bodies(bobs))
        self.assertIn("alice after the block", _bodies(carols))

    def test_each_still_sees_their_own(self) -> None:
        bobs, _ = group_thread_page(self._membership(self.bob))

        self.assertIn("bob after the block", _bodies(bobs))

    def test_the_older_page_cursor_cannot_reach_them_either(self) -> None:
        messages, _ = group_thread_page(self._membership(self.alice), before_id=self.carol_after.pk + 1000)

        self.assertNotIn(self.bob_after.pk, {message.pk for message in messages})

    def test_lifting_the_block_shows_them_again(self) -> None:
        """The cutoff is the live block's, so nothing is hidden once no block exists."""
        unblock_profile(self.alice, self.bob)

        messages, _ = group_thread_page(self._membership(self.alice))

        self.assertIn("bob after the block", _bodies(messages))


class InboxTests(_BlockedPairInAGroup):
    def _row(self, profile: Profile) -> dict:
        rows = group_conversations_for(profile)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_preview_skips_the_hidden_message(self) -> None:
        create_group_message(self.bob, self.group, "bob last word")

        row = self._row(self.alice)

        self.assertEqual(row["last_message"].pk, self.carol_after.pk)

    def test_the_unread_count_leaves_it_out(self) -> None:
        GroupMessage.objects.mark_read(self._membership(self.alice))
        create_group_message(self.bob, self.group, "bob again")

        self.assertEqual(self._row(self.alice)["unread_count"], 0)
        self.assertEqual(unread_group_conversation_count(self.alice), 0)
        self.assertEqual(unread_group_conversation_count(self.carol), 1)

    def test_the_member_count_leaves_the_other_out(self) -> None:
        self.assertEqual(self._row(self.alice)["member_count"], 2)
        self.assertEqual(self._row(self.bob)["member_count"], 2)
        self.assertEqual(self._row(self.carol)["member_count"], 3)


class NotificationTests(_BlockedPairInAGroup):
    def test_the_other_is_not_notified(self) -> None:
        NotificationLog.objects.all().delete()
        GroupMessage.objects.mark_read(self._membership(self.alice))
        GroupMessage.objects.mark_read(self._membership(self.carol))

        message = create_group_message(self.bob, self.group, "bob notifies")

        self.assertFalse(NotificationLog.objects.filter(profile=self.alice).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=self.carol, group_message=message).exists())

    def test_a_hidden_unread_message_does_not_suppress_a_visible_ones_notification(self) -> None:
        """The already-unread check must skip what the member cannot see, or it silences the next real message."""
        NotificationLog.objects.all().delete()
        GroupMessage.objects.mark_read(self._membership(self.alice))
        create_group_message(self.bob, self.group, "bob unread and hidden")

        message = create_group_message(self.carol, self.group, "carol wants alice")

        self.assertTrue(NotificationLog.objects.filter(profile=self.alice, group_message=message).exists())


class LiveDeliveryTests(_BlockedPairInAGroup):
    def _deliveries(self, action) -> list[tuple[str, dict]]:
        with (
            patch("urbanlens.dashboard.services.messaging.group_chats.send_group_messages") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            action()
        return [(group, event) for call in send.call_args_list for group, event in call.args[0]]

    def test_the_broadcast_skips_the_other(self) -> None:
        addressed = {
            group
            for group, _event in self._deliveries(lambda: create_group_message(self.bob, self.group, "live from bob"))
        }

        self.assertNotIn(direct_message_group_name(self.alice.pk), addressed)
        self.assertIn(direct_message_group_name(self.carol.pk), addressed)
        self.assertIn(direct_message_group_name(self.bob.pk), addressed)

    def test_a_deletion_is_not_announced_to_someone_who_never_saw_the_message(self) -> None:
        deliveries = self._deliveries(lambda: delete_group_message(self.bob_after, self.bob))

        addressed = {group for group, event in deliveries if event["message"]["type"] == "group_message_deleted"}
        self.assertNotIn(direct_message_group_name(self.alice.pk), addressed)
        self.assertIn(direct_message_group_name(self.carol.pk), addressed)

    def test_a_reaction_to_a_hidden_message_is_not_announced_to_them(self) -> None:
        deliveries = self._deliveries(lambda: toggle_group_reaction(self.carol, self.bob_after, "👍"))

        addressed = {group for group, _event in deliveries}
        self.assertNotIn(direct_message_group_name(self.alice.pk), addressed)
        self.assertIn(direct_message_group_name(self.bob.pk), addressed)

    def test_a_reaction_by_the_other_is_not_attributed_to_them(self) -> None:
        deliveries = self._deliveries(lambda: toggle_group_reaction(self.bob, self.carol_after, "👍"))

        to_alice = [
            event["message"] for group, event in deliveries if group == direct_message_group_name(self.alice.pk)
        ]
        for payload in to_alice:
            self.assertEqual(payload["reactions"], [], "Bob's reaction was relayed to Alice")


class ReactionTests(_BlockedPairInAGroup):
    def test_a_reaction_made_after_the_block_is_left_out_of_the_others_summary(self) -> None:
        Reaction.objects.create(profile=self.bob, group_message=self.carol_after, emoji="👍")
        message = GroupMessage.objects.prefetch_related("reactions__profile").get(pk=self.carol_after.pk)

        self.assertEqual(reaction_summary(message, viewer=self.alice), [])
        self.assertEqual(reaction_summary(message, viewer=self.carol)[0]["count"], 1)

    def test_a_reaction_made_before_the_block_stays(self) -> None:
        reaction = Reaction.objects.create(profile=self.bob, group_message=self.bob_before, emoji="👍")
        Reaction.objects.filter(pk=reaction.pk).update(created=timezone.now() - timedelta(minutes=20))
        message = GroupMessage.objects.prefetch_related("reactions__profile").get(pk=self.bob_before.pk)

        self.assertEqual(reaction_summary(message, viewer=self.alice)[0]["count"], 1)

    def test_nobody_can_react_to_a_message_they_cannot_see(self) -> None:
        with self.assertRaises(GroupChatPermissionError):
            toggle_group_reaction(self.alice, self.bob_after, "👍")

        self.assertFalse(Reaction.objects.filter(profile=self.alice).exists())


class WebEndpointTests(_BlockedPairInAGroup):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.alice.user)

    def test_the_thread_page_leaves_it_out(self) -> None:
        url = reverse("messages.group", kwargs={"group_uuid": self.group.uuid})
        # The full page renders only the sidebar; the thread arrives as the HTMX partial.
        full = self.client.get(url)
        partial = self.client.get(url, HTTP_HX_REQUEST="true")

        self.assertEqual(full.status_code, 200)
        self.assertNotContains(full, "bob after the block")
        self.assertNotContains(partial, "bob after the block")
        self.assertContains(partial, "bob before the block")

    def test_the_header_counts_only_the_members_they_can_see(self) -> None:
        response = self.client.get(
            reverse("messages.group", kwargs={"group_uuid": self.group.uuid}), HTTP_HX_REQUEST="true"
        )

        self.assertContains(response, "2 members")

    def test_the_older_page_leaves_it_out(self) -> None:
        response = self.client.get(
            reverse("messages.group.older", kwargs={"group_uuid": self.group.uuid}),
            {"before": self.carol_after.pk + 1000},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "bob after the block")

    def test_sending_re_renders_without_it(self) -> None:
        response = self.client.post(
            reverse("messages.group.send", kwargs={"group_uuid": self.group.uuid}), {"body": "alice speaks"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "bob after the block")

    def test_the_member_list_leaves_the_other_out(self) -> None:
        response = self.client.get(reverse("messages.group.members", kwargs={"group_uuid": self.group.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="dm-group-member-row"', count=2)
        self.assertNotContains(response, self.bob.user.username)
        self.assertContains(response, self.carol.user.username)

    def test_the_other_side_sees_the_same_absence(self) -> None:
        self.client.force_login(self.bob.user)

        response = self.client.get(reverse("messages.group.members", kwargs={"group_uuid": self.group.uuid}))

        self.assertContains(response, 'class="dm-group-member-row"', count=2)

    def test_a_hidden_message_answers_delete_as_a_missing_one(self) -> None:
        response = self.client.post(
            reverse("messages.group.delete", kwargs={"group_uuid": self.group.uuid, "message_id": self.bob_after.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_a_hidden_message_answers_share_respond_as_a_missing_one(self) -> None:
        url = reverse(
            "messages.group.share.pin.respond", kwargs={"group_uuid": self.group.uuid, "message_id": self.bob_after.pk}
        )

        self.assertEqual(self.client.post(url, {"action": "accept"}).status_code, 404)


class ExternalApiTests(_BlockedPairInAGroup):
    def setUp(self) -> None:
        super().setUp()
        self.auth = _bearer(self.alice.user)

    def test_the_thread_leaves_it_out(self) -> None:
        response = self.client.get(
            reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid}), **self.auth
        )

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.json()["results"]}
        self.assertNotIn(self.bob_after.pk, ids)
        self.assertIn(self.bob_before.pk, ids)

    def test_the_inbox_preview_and_count_leave_it_out(self) -> None:
        create_group_message(self.bob, self.group, "bob last word")

        conversations = self.client.get(reverse("external_api:messages.conversations"), **self.auth).json()["results"]
        groups = self.client.get(reverse("external_api:messages.groups"), **self.auth).json()["results"]

        [conversation] = [row for row in conversations if row["kind"] == "group"]
        self.assertEqual(conversation["last_message"]["id"], self.carol_after.pk)
        self.assertEqual(conversation["member_count"], 2)
        self.assertEqual(groups[0]["member_count"], 2)

    def test_the_member_list_leaves_the_other_out(self) -> None:
        response = self.client.get(
            reverse("external_api:messages.groups.members", kwargs={"group_uuid": self.group.uuid}), **self.auth
        )

        self.assertEqual(response.status_code, 200)
        slugs = {row["slug"] for row in response.json()}
        self.assertNotIn(self.bob.slug, slugs)
        self.assertEqual(len(response.json()), 2)

    def test_reacting_to_a_hidden_message_answers_as_a_missing_one(self) -> None:
        response = self.client.post(
            reverse(
                "external_api:messages.groups.messages.react",
                kwargs={"group_uuid": self.group.uuid, "message_id": self.bob_after.pk},
            ),
            data=json.dumps({"emoji": "👍"}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reaction.objects.filter(profile=self.alice).exists())

    def test_deleting_a_hidden_message_answers_as_a_missing_one(self) -> None:
        """403 ("only the sender") would confirm the id is a message in this group."""
        response = self.client.delete(
            reverse(
                "external_api:messages.groups.messages.detail",
                kwargs={"group_uuid": self.group.uuid, "message_id": self.bob_after.pk},
            ),
            **self.auth,
        )

        self.assertEqual(response.status_code, 404)

    def test_a_visible_reaction_summary_leaves_the_others_reaction_out(self) -> None:
        Reaction.objects.create(profile=self.bob, group_message=self.carol_after, emoji="🔥")

        response = self.client.get(
            reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid}), **self.auth
        )

        [row] = [row for row in response.json()["results"] if row["id"] == self.carol_after.pk]
        self.assertEqual(row["reactions"], [])


class CiphertextIsUnreachableTests(_BlockedPairInAGroup):
    """In an encrypted group the key still reaches both, so the server withholding the ciphertext is what hides it."""

    def setUp(self) -> None:
        super().setUp()
        GroupKey.objects.create(group=self.group, version=1)
        self.ciphertext = _blob(b"hidden-from-alice" + os.urandom(24))
        self.hidden = create_group_message(
            self.bob, self.group, "", ciphertext=self.ciphertext, nonce=_blob(b"\x03" * 24), key_version=1
        )
        self.client.force_login(self.alice.user)
        self.auth = _bearer(self.alice.user)

    def _assert_absent(self, response) -> None:
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertNotIn(self.ciphertext, response.content.decode())

    def test_no_web_page_carries_it(self) -> None:
        self._assert_absent(self.client.get(reverse("messages.group", kwargs={"group_uuid": self.group.uuid})))
        self._assert_absent(
            self.client.get(reverse("messages.group", kwargs={"group_uuid": self.group.uuid}), HTTP_HX_REQUEST="true")
        )
        self._assert_absent(
            self.client.get(
                reverse("messages.group.older", kwargs={"group_uuid": self.group.uuid}), {"before": self.hidden.pk + 1}
            )
        )
        self._assert_absent(self.client.get(reverse("messages.view")))

    def test_no_api_endpoint_carries_it(self) -> None:
        self._assert_absent(
            self.client.get(
                reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid}), **self.auth
            )
        )
        self._assert_absent(
            self.client.get(
                reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid}),
                {"before": self.hidden.pk + 1},
                **self.auth,
            )
        )
        self._assert_absent(self.client.get(reverse("external_api:messages.conversations"), **self.auth))
        self._assert_absent(self.client.get(reverse("external_api:messages.groups"), **self.auth))
        self._assert_absent(self.client.get(reverse("e2ee.group_key", kwargs={"group_uuid": self.group.uuid})))

    def test_it_is_not_pushed_live(self) -> None:
        with (
            patch("urbanlens.dashboard.services.messaging.group_chats.send_group_messages") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            create_group_message(
                self.bob, self.group, "", ciphertext=self.ciphertext, nonce=_blob(b"\x04" * 24), key_version=1
            )

        to_alice = [
            event
            for call in send.call_args_list
            for group, event in call.args[0]
            if group == direct_message_group_name(self.alice.pk)
        ]
        self.assertEqual(to_alice, [])


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


class WebSocketTests(TransactionTestCase):
    """Through the real consumer: a frame Bob sends reaches Carol's socket and never Alice's."""

    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.carol = _profile("carol")
        self.group = create_group_chat(self.carol, "Quarry crew", [self.alice, self.bob])
        block_profile(self.bob, self.alice)
        GroupKey.objects.create(group=self.group, version=1)
        self.ciphertext = _blob(b"socket-secret" + os.urandom(24))

    def _communicator(self, profile: Profile) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(DirectMessageConsumer.as_asgi(), "/ws/messages/")
        comm.scope["url_route"] = {"kwargs": {}}
        comm.scope["user"] = profile.user
        return comm

    @database_sync_to_async
    def _sent(self) -> int:
        return GroupMessage.objects.filter(sender=self.bob).count()

    async def _frames(self, comm: WebsocketCommunicator) -> list[dict]:
        frames = []
        while not await comm.receive_nothing(timeout=0.5):
            frames.append(json.loads(await comm.receive_from()))
        return frames

    def test_a_live_frame_reaches_the_group_but_not_the_other(self) -> None:
        with tasks_run_inline(broadcast_channel_group_messages):
            _run(self._exchange())

    async def _exchange(self) -> None:
        alice, bob, carol = self._communicator(self.alice), self._communicator(self.bob), self._communicator(self.carol)
        for comm in (alice, bob, carol):
            connected, _ = await comm.connect()
            self.assertTrue(connected)

        await bob.send_to(text_data=json.dumps({"group": str(self.group.uuid), "body": "socket hello"}))
        await bob.send_to(
            text_data=json.dumps(
                {
                    "group": str(self.group.uuid),
                    "ciphertext": self.ciphertext,
                    "nonce": _blob(b"\x05" * 24),
                    "key_version": 1,
                }
            )
        )

        carols = await self._frames(carol)
        alices = await self._frames(alice)
        await self._frames(bob)

        self.assertEqual(await self._sent(), 2)
        self.assertIn("socket hello", {frame.get("body") for frame in carols if frame.get("type") == "group_message"})
        self.assertIn(
            self.ciphertext, {frame.get("ciphertext") for frame in carols if frame.get("type") == "group_message"}
        )
        self.assertEqual([frame for frame in alices if frame.get("type") == "group_message"], [])
        self.assertNotIn(self.ciphertext, json.dumps(alices))
        for comm in (alice, bob, carol):
            await comm.disconnect()
