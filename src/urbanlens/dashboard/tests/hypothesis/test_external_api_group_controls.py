"""Tests for the external API's group-chat controls and per-conversation mute.

These endpoints closed a set of gaps where the one-to-one conversation had a
capability and the group did not: reacting to a message, deleting one's own
message, leaving, and muting. The asymmetry itself was the bug, so most of what
is asserted here is that the group behaves *the same way* the direct-message
surface already does - and that the handful of places it deliberately differs
differ for a stated reason.

Four properties get the most attention, in rough order of how bad the failure
would be:

- **Group scoping of message ids.** ``GroupMessage`` primary keys are
  sequential across the whole table, so a lookup that forgot ``group=`` would
  let a member of any one group react into - or read the existence of - every
  other group's messages. Both message endpoints are tested against an id from
  a group the caller is a member of, which is the case a ``pk``-only lookup
  passes and a scoped lookup rejects.
- **Declarative mute.** PUT and DELETE must be idempotent. A toggle would mean
  a retried request over a flaky mobile link silently inverts the state the
  first, unacknowledged attempt applied, and the user then gets back exactly
  the notifications they silenced.
- **Mute is notification-only.** A muted conversation stays in the conversation
  list. "Muted" quietly becoming "hidden" would lose people their threads.
- **``update_fields`` on the group mute write.** The membership row also
  carries ``left_at``/``removed_by``; a full ``save()`` would write back stale
  copies of those and resurrect a membership a concurrent removal just ended.
"""

from __future__ import annotations

from datetime import timedelta
import json
import os

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.messaging.group_chats import (
    add_group_members,
    create_group_chat,
    set_group_muted,
    share_pin_in_group_message,
)

AccessToken = get_access_token_model()

READ_WRITE = f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}"


def _bearer(raw: str) -> dict:
    """Request kwargs carrying a credential as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _profile() -> Profile:
    """Make a fresh user and return their profile."""
    return baker.make(User).profile


def _token_for(user: User, scope: str = READ_WRITE) -> str:
    """Issue a first-party OAuth2 access token.

    Messaging scopes are in ``permissions.OAUTH2_ONLY_SCOPES``, so an OAuth2
    token is the only credential that can reach any of these endpoints - a PAT
    is refused even holding the same scope strings (asserted below).
    """
    token = AccessToken.objects.create(
        user=user,
        application=first_party_application(),
        token=f"tok-{os.urandom(8).hex()}",
        expires=timezone.now() + timedelta(hours=1),
        scope=scope,
    )
    return token.token


def _open_dms(*profiles: Profile) -> None:
    """Let these profiles message each other, which group creation requires."""
    Profile.objects.filter(pk__in=[profile.pk for profile in profiles]).update(
        direct_message_visibility=VisibilityChoice.ANYONE
    )
    for profile in profiles:
        profile.refresh_from_db()


def _befriend(first: Profile, second: Profile) -> None:
    """Connect two profiles with exactly one Friendship row.

    One row, never one per direction: ``Friendship.objects.between()`` resolves
    the pair with ``.get()``, so a reciprocal second row makes every connection
    check raise ``MultipleObjectsReturned``.
    """
    Friendship.objects.create(
        from_profile=first,
        to_profile=second,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class GroupControlsBaseTestCase(TestCase):
    """A three-person group, with OAuth2 tokens for the creator and one member."""

    def setUp(self) -> None:
        """Create the cast, open their DM privacy, and start a group."""
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.creator = _profile()
        self.member = _profile()
        self.stranger = _profile()
        _open_dms(self.creator, self.member, self.stranger)

        self.group = create_group_chat(self.creator, "Crew", [self.member])
        self.creator_auth = _bearer(_token_for(self.creator.user))
        self.member_auth = _bearer(_token_for(self.member.user))
        self.stranger_auth = _bearer(_token_for(self.stranger.user))

    def _message(
        self, sender: Profile | None = None, body: str = "hello", group: GroupChat | None = None
    ) -> GroupMessage:
        """Put one message in a group without going through the send endpoint."""
        return GroupMessage.objects.create(group=group or self.group, sender=sender or self.creator, body=body)

    def _post_json(self, url: str, payload: dict, auth: dict) -> object:
        """POST a JSON body with the given bearer credential."""
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", **auth)


class GroupMessageReactionTests(GroupControlsBaseTestCase):
    """POST /messages/groups/<uuid>/messages/<id>/react/."""

    def setUp(self) -> None:
        """Add a message from the creator for members to react to."""
        super().setUp()
        self.message = self._message()
        self.url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": self.message.pk},
        )

    def test_first_call_adds_and_second_removes(self) -> None:
        """The endpoint toggles, and reports which way it went."""
        added = self._post_json(self.url, {"emoji": "👍"}, self.member_auth)
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()["action"], "added")
        self.assertEqual(added.json()["reactions"], [{"emoji": "👍", "count": 1, "slugs": [self.member.slug or ""]}])

        removed = self._post_json(self.url, {"emoji": "👍"}, self.member_auth)
        self.assertEqual(removed.json()["action"], "removed")
        self.assertEqual(removed.json()["reactions"], [])
        self.assertFalse(Reaction.objects.filter(group_message=self.message).exists())

    def test_reaction_is_recorded_against_the_group_message_host(self) -> None:
        """The row lands on ``group_message``, not on some other reactable host.

        Reaction is one table with a nullable FK per host; writing to the wrong
        one would still "work" and would then aggregate against a stranger.
        """
        self._post_json(self.url, {"emoji": "🔥"}, self.member_auth)
        reaction = Reaction.objects.get(profile=self.member, emoji="🔥")
        self.assertEqual(reaction.group_message_id, self.message.pk)
        self.assertIsNone(reaction.direct_message_id)
        self.assertIsNone(reaction.comment_id)

    def test_two_members_reacting_with_the_same_emoji_aggregate(self) -> None:
        """The per-host unique constraint is per profile, not per message."""
        self._post_json(self.url, {"emoji": "🎉"}, self.member_auth)
        response = self._post_json(self.url, {"emoji": "🎉"}, self.creator_auth)
        self.assertEqual(response.json()["reactions"][0]["count"], 2)

    def test_unusable_emoji_is_refused(self) -> None:
        """A glyph that is relayed verbatim into other clients must be render-safe."""
        response = self._post_json(self.url, {"emoji": "<script>"}, self.member_auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "That isn't a usable reaction.")
        self.assertFalse(Reaction.objects.filter(group_message=self.message).exists())

    def test_unusable_emoji_is_refused_before_the_message_is_looked_up(self) -> None:
        """A junk emoji must not become a probe for which message ids exist.

        Resolving the message first would answer 404 for an id the caller
        cannot see and 400 for one they can, which is an oracle.
        """
        url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": self.message.pk + 10_000},
        )
        self.assertEqual(self._post_json(url, {"emoji": "<script>"}, self.member_auth).status_code, 400)

    def test_unknown_group_is_404(self) -> None:
        """An unknown uuid answers like any other group route."""
        url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": "11111111-1111-1111-1111-111111111111", "message_id": self.message.pk},
        )
        self.assertEqual(self._post_json(url, {"emoji": "👍"}, self.member_auth).status_code, 404)

    def test_non_member_is_404_not_403(self) -> None:
        """A stranger must not be able to confirm the group or the message exists."""
        self.assertEqual(self._post_json(self.url, {"emoji": "👍"}, self.stranger_auth).status_code, 404)

    def test_removed_member_can_no_longer_react(self) -> None:
        """An ended membership row is not membership.

        The row survives a removal (it records the window they could see), so a
        guard that merely looked for a row would keep letting them react.
        """
        self.group.membership_for(self.member).end(removed_by=self.creator)
        self.assertEqual(self._post_json(self.url, {"emoji": "👍"}, self.member_auth).status_code, 404)

    def test_message_id_from_another_group_is_404(self) -> None:
        """The lookup is scoped to the group in the path, not to the id alone.

        The caller is an active member of *both* groups, so only the ``group=``
        term in the lookup can reject this - which is exactly the term a
        ``pk``-only implementation omits.
        """
        other_group = create_group_chat(self.creator, "Other", [self.member])
        elsewhere = self._message(group=other_group, body="not yours")
        url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": elsewhere.pk},
        )
        self.assertEqual(self._post_json(url, {"emoji": "👍"}, self.member_auth).status_code, 404)
        self.assertFalse(Reaction.objects.filter(group_message=elsewhere).exists())

    def test_pat_credential_is_refused_even_holding_the_scope(self) -> None:
        """Messaging is OAuth2-only; a leaked bearer key is not a way in."""
        key, raw = generate_api_key(self.member.user, "leaky-key")
        ApiKey.objects.filter(pk=key.pk).update(
            scopes=[ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.MESSAGES_WRITE.value]
        )
        self.assertEqual(self._post_json(self.url, {"emoji": "👍"}, _bearer(raw)).status_code, 403)

    def test_read_scope_alone_cannot_react(self) -> None:
        """Reacting is a write."""
        auth = _bearer(_token_for(self.member.user, ApiKeyScope.MESSAGES_READ.value))
        self.assertEqual(self._post_json(self.url, {"emoji": "👍"}, auth).status_code, 403)


class GroupMessageDeleteTests(GroupControlsBaseTestCase):
    """DELETE /messages/groups/<uuid>/messages/<id>/."""

    def setUp(self) -> None:
        """Add a message sent by the creator."""
        super().setUp()
        self.message = self._message(body="oops")
        self.url = reverse(
            "external_api:messages.groups.messages.detail",
            kwargs={"group_uuid": self.group.uuid, "message_id": self.message.pk},
        )

    def test_sender_deletes_for_everyone(self) -> None:
        """The tombstone is set, and the other members stop seeing the body."""
        response = self.client.delete(self.url, **self.creator_auth)
        self.assertEqual(response.status_code, 204)
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.deleted_at)
        self.assertIsNone(self.message.tombstone_text_for(self.creator.pk), "the sender keeps seeing their own message")
        self.assertEqual(self.message.tombstone_text_for(self.member.pk), "Message deleted")

    def test_repeat_delete_is_204(self) -> None:
        """Idempotent: a retried delete is not an error."""
        first = self.client.delete(self.url, **self.creator_auth)
        second = self.client.delete(self.url, **self.creator_auth)
        self.assertEqual((first.status_code, second.status_code), (204, 204))

    def test_non_sender_member_gets_403(self) -> None:
        """A deliberate 403: the caller was provably already shown this message."""
        response = self.client.delete(self.url, **self.member_auth)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "Only the sender can delete this message.")
        self.message.refresh_from_db()
        self.assertIsNone(self.message.deleted_at)

    def test_unknown_message_id_is_404(self) -> None:
        """An id that isn't in this group answers 404, not 403."""
        url = reverse(
            "external_api:messages.groups.messages.detail",
            kwargs={"group_uuid": self.group.uuid, "message_id": self.message.pk + 10_000},
        )
        self.assertEqual(self.client.delete(url, **self.creator_auth).status_code, 404)

    def test_message_id_from_another_group_is_404(self) -> None:
        """Scoped to the group in the path even when the caller sent the message."""
        other_group = create_group_chat(self.creator, "Other", [self.member])
        elsewhere = self._message(group=other_group, body="not this thread")
        url = reverse(
            "external_api:messages.groups.messages.detail",
            kwargs={"group_uuid": self.group.uuid, "message_id": elsewhere.pk},
        )
        self.assertEqual(self.client.delete(url, **self.creator_auth).status_code, 404)
        elsewhere.refresh_from_db()
        self.assertIsNone(elsewhere.deleted_at)

    def test_non_member_is_404(self) -> None:
        """A stranger cannot confirm the group exists, let alone the message."""
        self.assertEqual(self.client.delete(self.url, **self.stranger_auth).status_code, 404)

    def test_scope_query_param_is_ignored(self) -> None:
        """There is no group analogue of the one-to-one ``?scope=self``.

        A group message has no per-member copy to hide, so honouring the
        parameter would mean inventing semantics that diverge from the
        direct-message contract clients already implement. It is accepted-and-
        ignored rather than rejected, so a client that sends its DM parameters
        verbatim still gets the delete it asked for.
        """
        response = self.client.delete(f"{self.url}?scope=self", **self.creator_auth)
        self.assertEqual(response.status_code, 204)
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.deleted_at, "scope=self must not become a per-member hide")


class GroupLeaveTests(GroupControlsBaseTestCase):
    """POST /messages/groups/<uuid>/leave/."""

    def setUp(self) -> None:
        """Resolve the leave URL for the fixture group."""
        super().setUp()
        self.url = reverse("external_api:messages.groups.leave", kwargs={"group_uuid": self.group.uuid})

    def test_member_leaves(self) -> None:
        """The stint ends, without a remover recorded."""
        response = self.client.post(self.url, **self.member_auth)
        self.assertEqual(response.status_code, 204)
        membership = GroupChatMembership.objects.get(group=self.group, profile=self.member)
        self.assertIsNotNone(membership.left_at)
        self.assertIsNone(membership.removed_by, "a voluntary leave records no remover")
        self.assertIsNone(self.group.membership_for(self.member))

    def test_repeat_leave_is_204(self) -> None:
        """Idempotent for someone who was a member - a lost response is not a failure.

        Answering the retry 404 would be indistinguishable from "that group
        never existed", which is precisely the ambiguity a retrying mobile
        client cannot resolve.
        """
        first = self.client.post(self.url, **self.member_auth)
        second = self.client.post(self.url, **self.member_auth)
        self.assertEqual((first.status_code, second.status_code), (204, 204))
        self.assertEqual(GroupChatMembership.objects.filter(group=self.group, profile=self.member).count(), 1)

    def test_never_a_member_is_404(self) -> None:
        """A stranger must not learn whether the uuid names a real group."""
        self.assertEqual(self.client.post(self.url, **self.stranger_auth).status_code, 404)

    def test_unknown_group_is_404(self) -> None:
        """An unknown uuid answers the same as a group the caller can't see."""
        url = reverse(
            "external_api:messages.groups.leave", kwargs={"group_uuid": "11111111-1111-1111-1111-111111111111"}
        )
        self.assertEqual(self.client.post(url, **self.member_auth).status_code, 404)

    def test_creator_may_leave_their_own_group(self) -> None:
        """Leaving is open to every member, including the creator."""
        self.assertEqual(self.client.post(self.url, **self.creator_auth).status_code, 204)

    def test_leaver_loses_access_to_the_thread(self) -> None:
        """Leaving is not cosmetic: the thread stops resolving for them."""
        self.client.post(self.url, **self.member_auth)
        thread = reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid})
        self.assertEqual(self.client.get(thread, **self.member_auth).status_code, 404)

    def test_read_scope_alone_cannot_leave(self) -> None:
        """Leaving is a write."""
        auth = _bearer(_token_for(self.member.user, ApiKeyScope.MESSAGES_READ.value))
        self.assertEqual(self.client.post(self.url, **auth).status_code, 403)


class GroupMuteTests(GroupControlsBaseTestCase):
    """GET/PUT/DELETE /messages/groups/<uuid>/mute/."""

    def setUp(self) -> None:
        """Resolve the group mute URL."""
        super().setUp()
        self.url = reverse("external_api:messages.groups.mute", kwargs={"group_uuid": self.group.uuid})

    def _muted(self) -> bool:
        """Read the member's persisted mute flag straight from the database."""
        return GroupChatMembership.objects.get(group=self.group, profile=self.member).muted

    def test_put_mutes_and_delete_unmutes(self) -> None:
        """The two verbs name end states, and the body reports the persisted one."""
        muted = self.client.put(self.url, **self.member_auth)
        self.assertEqual(muted.status_code, 200)
        self.assertEqual(muted.json(), {"is_muted": True})
        self.assertTrue(self._muted())

        unmuted = self.client.delete(self.url, **self.member_auth)
        self.assertEqual(unmuted.json(), {"is_muted": False})
        self.assertFalse(self._muted())

    def test_repeated_put_does_not_invert(self) -> None:
        """The whole reason this is not a toggling POST."""
        self.client.put(self.url, **self.member_auth)
        second = self.client.put(self.url, **self.member_auth)
        self.assertEqual(second.json(), {"is_muted": True})
        self.assertTrue(self._muted())

    def test_repeated_delete_does_not_invert(self) -> None:
        """Unmuting an already-unmuted group is a no-op, not a mute."""
        first = self.client.delete(self.url, **self.member_auth)
        second = self.client.delete(self.url, **self.member_auth)
        self.assertEqual((first.json(), second.json()), ({"is_muted": False}, {"is_muted": False}))
        self.assertFalse(self._muted())

    def test_get_reports_state(self) -> None:
        """The optional read verb, so a client can render the control on first paint."""
        self.assertEqual(self.client.get(self.url, **self.member_auth).json(), {"is_muted": False})
        self.client.put(self.url, **self.member_auth)
        self.assertEqual(self.client.get(self.url, **self.member_auth).json(), {"is_muted": True})

    def test_mute_is_per_member(self) -> None:
        """One member muting must not silence the group for anyone else."""
        self.client.put(self.url, **self.member_auth)
        self.assertEqual(self.client.get(self.url, **self.creator_auth).json(), {"is_muted": False})

    def test_muted_group_stays_in_the_conversation_list(self) -> None:
        """Muting is notification-only. A hidden thread is a lost thread."""
        self._message(body="still here")
        self.client.put(self.url, **self.member_auth)
        payload = self.client.get(reverse("external_api:messages.conversations"), **self.member_auth).json()
        groups = [
            row for row in payload["results"] if row["kind"] == "group" and row["group_uuid"] == str(self.group.uuid)
        ]
        self.assertEqual(len(groups), 1, "a muted group must still be listed")
        self.assertTrue(groups[0]["is_muted"])
        self.assertEqual(groups[0]["unread_count"], 1, "muting must not zero the unread count either")

    def test_non_member_is_404(self) -> None:
        """Both the read and the write verbs refuse a stranger the same way."""
        self.assertEqual(self.client.get(self.url, **self.stranger_auth).status_code, 404)
        self.assertEqual(self.client.put(self.url, **self.stranger_auth).status_code, 404)
        self.assertEqual(self.client.delete(self.url, **self.stranger_auth).status_code, 404)

    def test_unknown_group_is_404(self) -> None:
        """An unknown uuid is indistinguishable from a group the caller can't see."""
        url = reverse(
            "external_api:messages.groups.mute", kwargs={"group_uuid": "11111111-1111-1111-1111-111111111111"}
        )
        self.assertEqual(self.client.put(url, **self.member_auth).status_code, 404)

    def test_read_scope_alone_cannot_write_the_flag(self) -> None:
        """GET is a read; PUT and DELETE are not."""
        auth = _bearer(_token_for(self.member.user, ApiKeyScope.MESSAGES_READ.value))
        self.assertEqual(self.client.get(self.url, **auth).status_code, 200)
        self.assertEqual(self.client.put(self.url, **auth).status_code, 403)
        self.assertEqual(self.client.delete(self.url, **auth).status_code, 403)


class SetGroupMutedServiceTests(GroupControlsBaseTestCase):
    """``services.messaging.group_chats.set_group_muted`` writes narrowly."""

    def test_write_does_not_clobber_a_concurrent_removal(self) -> None:
        """``update_fields`` is load-bearing, not a micro-optimization.

        The membership row carries ``left_at``/``removed_by`` alongside
        ``muted``. A full ``save()`` from a stale in-memory copy - which is
        what a request that loaded the row before a concurrent removal holds -
        would write back ``left_at=None`` and silently readmit someone who had
        just been removed from the group.
        """
        stale = GroupChatMembership.objects.get(group=self.group, profile=self.member)
        # A concurrent request removes them while `stale` still says active.
        GroupChatMembership.objects.get(pk=stale.pk).end(removed_by=self.creator)

        set_group_muted(stale, muted=True)

        fresh = GroupChatMembership.objects.get(pk=stale.pk)
        self.assertIsNotNone(fresh.left_at, "the removal must survive the mute write")
        self.assertEqual(fresh.removed_by_id, self.creator.pk)
        self.assertTrue(fresh.muted)


class ConversationMuteTests(GroupControlsBaseTestCase):
    """GET/PUT/DELETE /messages/<peer_slug>/mute/."""

    def setUp(self) -> None:
        """Resolve the one-to-one mute URL for the creator's view of the member."""
        super().setUp()
        self.url = reverse("external_api:messages.mute", kwargs={"peer_slug": self.member.ensure_slug()})

    def _muted(self) -> bool:
        """Read the persisted mute state straight from the database."""
        return DirectMessageMute.objects.filter(viewer=self.creator, sender=self.member).exists()

    def test_put_mutes_and_delete_unmutes(self) -> None:
        """Row existence is the mute state; the verbs drive it declaratively."""
        muted = self.client.put(self.url, **self.creator_auth)
        self.assertEqual(muted.status_code, 200)
        self.assertEqual(muted.json(), {"is_muted": True})
        self.assertTrue(self._muted())

        unmuted = self.client.delete(self.url, **self.creator_auth)
        self.assertEqual(unmuted.json(), {"is_muted": False})
        self.assertFalse(self._muted())

    def test_repeated_put_does_not_invert_or_duplicate(self) -> None:
        """A retry must neither un-mute nor trip the unique constraint."""
        self.client.put(self.url, **self.creator_auth)
        second = self.client.put(self.url, **self.creator_auth)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"is_muted": True})
        self.assertEqual(DirectMessageMute.objects.filter(viewer=self.creator, sender=self.member).count(), 1)

    def test_repeated_delete_does_not_invert(self) -> None:
        """Unmuting an unmuted conversation stays unmuted."""
        first = self.client.delete(self.url, **self.creator_auth)
        second = self.client.delete(self.url, **self.creator_auth)
        self.assertEqual((first.json(), second.json()), ({"is_muted": False}, {"is_muted": False}))

    def test_get_reports_state(self) -> None:
        """The optional read verb."""
        self.assertEqual(self.client.get(self.url, **self.creator_auth).json(), {"is_muted": False})
        self.client.put(self.url, **self.creator_auth)
        self.assertEqual(self.client.get(self.url, **self.creator_auth).json(), {"is_muted": True})

    def test_mute_is_directional(self) -> None:
        """Muting someone does not mute you for them."""
        self.client.put(self.url, **self.creator_auth)
        reverse_url = reverse("external_api:messages.mute", kwargs={"peer_slug": self.creator.ensure_slug()})
        self.assertEqual(self.client.get(reverse_url, **self.member_auth).json(), {"is_muted": False})

    def test_reserved_peer_slug_is_404(self) -> None:
        """A profile whose slug collides with a route literal is not a peer.

        ``messages/<peer_slug>/`` is a catch-all, so a user who managed to hold
        the slug "groups" would otherwise be addressable at a path that reads
        like the group namespace.
        """
        for reserved in ("groups", "settings", "conversations"):
            url = reverse("external_api:messages.mute", kwargs={"peer_slug": reserved})
            self.assertEqual(self.client.put(url, **self.creator_auth).status_code, 404, reserved)

    def test_unknown_peer_is_404(self) -> None:
        """No such profile, no such conversation."""
        url = reverse("external_api:messages.mute", kwargs={"peer_slug": "nobody-at-all"})
        self.assertEqual(self.client.get(url, **self.creator_auth).status_code, 404)

    def test_muted_conversation_stays_in_the_conversation_list(self) -> None:
        """Muting is notification-only for one-to-one threads too."""
        from urbanlens.dashboard.services.messaging.direct_messages import create_direct_message

        create_direct_message(self.member, self.creator, "hi")
        self.client.put(self.url, **self.creator_auth)
        payload = self.client.get(reverse("external_api:messages.conversations"), **self.creator_auth).json()
        rows = [row for row in payload["results"] if row["kind"] == "dm" and row["peer_slug"] == self.member.slug]
        self.assertEqual(len(rows), 1, "a muted conversation must still be listed")
        self.assertTrue(rows[0]["is_muted"])

    def test_read_scope_alone_cannot_write_the_flag(self) -> None:
        """GET is a read; PUT and DELETE are not."""
        auth = _bearer(_token_for(self.creator.user, ApiKeyScope.MESSAGES_READ.value))
        self.assertEqual(self.client.get(self.url, **auth).status_code, 200)
        self.assertEqual(self.client.put(self.url, **auth).status_code, 403)

    def test_pat_credential_is_refused(self) -> None:
        """Messaging stays OAuth2-only on the new routes as well."""
        key, raw = generate_api_key(self.creator.user, "leaky-key")
        ApiKey.objects.filter(pk=key.pk).update(
            scopes=[ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.MESSAGES_WRITE.value]
        )
        self.assertEqual(self.client.put(self.url, **_bearer(raw)).status_code, 403)


class GroupMessagePayloadTests(GroupControlsBaseTestCase):
    """What ``build_group_message_payload`` now emits over the thread endpoint."""

    def setUp(self) -> None:
        """Give the creator a pin and connect them to the member."""
        super().setUp()
        _befriend(self.creator, self.member)
        self.location = baker.make(Location, latitude=42.3, longitude=-83.0)
        self.pin = baker.make(Pin, profile=self.creator, location=self.location)
        self.thread_url = reverse("external_api:messages.groups.detail", kwargs={"group_uuid": self.group.uuid})

    def _thread_rows(self, auth: dict) -> list[dict]:
        """Fetch the group thread as one caller."""
        response = self.client.get(self.thread_url, **auth)
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_recipient_sees_the_pin_share_id_they_must_respond_to(self) -> None:
        """Without this field the share card has no id to accept or reject.

        The recipient's own ``PinShare`` is the one they may answer at
        ``/pin-shares/<id>/respond/``; a group share creates one per member, so
        there is no single shared id to report.
        """
        message = share_pin_in_group_message(self.creator, self.group, self.pin, "look at this")
        share = message.shares.get(recipient=self.member)

        rows = self._thread_rows(self.member_auth)
        row = next(item for item in rows if item["id"] == message.pk)
        self.assertEqual(row["pin_share_id"], share.pin_share_id)
        self.assertIsNotNone(row["pin_share_id"])

    def test_the_sender_gets_no_pin_share_id_of_their_own(self) -> None:
        """A share is addressed to the other members; the sender has nothing to accept."""
        message = share_pin_in_group_message(self.creator, self.group, self.pin, "look at this")
        rows = self._thread_rows(self.creator_auth)
        row = next(item for item in rows if item["id"] == message.pk)
        self.assertIsNone(row["pin_share_id"])

    def test_a_plain_message_reports_a_null_pin_share_id(self) -> None:
        """The field is always present, so clients need no shape branch."""
        message = self._message(body="no share here")
        row = next(item for item in self._thread_rows(self.member_auth) if item["id"] == message.pk)
        self.assertIsNone(row["pin_share_id"])

    def test_reactions_surface_in_the_thread(self) -> None:
        """The payload used to hard-code an empty reaction list."""
        message = self._message(body="react to me")
        react_url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": message.pk},
        )
        self._post_json(react_url, {"emoji": "❤️"}, self.member_auth)

        row = next(item for item in self._thread_rows(self.creator_auth) if item["id"] == message.pk)
        self.assertEqual(row["reactions"], [{"emoji": "❤️", "count": 1, "slugs": [self.member.slug or ""]}])

    def test_reactions_survive_the_conversation_list_serializer(self) -> None:
        """The inbox row runs the payload through ``DirectMessageSerializer``.

        A field the builder emits but the serializer does not declare is
        silently dropped there while still appearing in the thread - the kind
        of surface-dependent shape drift that costs a client author an
        afternoon.
        """
        message = self._message(body="last word")
        react_url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": message.pk},
        )
        self._post_json(react_url, {"emoji": "🔥"}, self.member_auth)

        payload = self.client.get(reverse("external_api:messages.conversations"), **self.creator_auth).json()
        row = next(
            item
            for item in payload["results"]
            if item["kind"] == "group" and item["group_uuid"] == str(self.group.uuid)
        )
        self.assertEqual(
            row["last_message"]["reactions"], [{"emoji": "🔥", "count": 1, "slugs": [self.member.slug or ""]}]
        )
        self.assertIn("pin_share_id", row["last_message"])

    def test_thread_reactions_mask_hidden_reactor_slug_per_viewer(self) -> None:
        """A masked group member's reaction still counts without exposing their profile slug."""
        hidden = _profile()
        _open_dms(self.creator, hidden)
        add_group_members(self.group, self.creator, [hidden])
        Profile.objects.filter(pk=hidden.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        hidden.refresh_from_db()
        hidden.ensure_slug()
        hidden_auth = _bearer(_token_for(hidden.user))

        message = self._message(body="react privately")
        react_url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": message.pk},
        )
        self._post_json(react_url, {"emoji": "🔥"}, hidden_auth)

        creator_row = next(item for item in self._thread_rows(self.creator_auth) if item["id"] == message.pk)
        hidden_row = next(item for item in self._thread_rows(hidden_auth) if item["id"] == message.pk)
        self.assertEqual(creator_row["reactions"], [{"emoji": "🔥", "count": 1, "slugs": [""]}])
        self.assertEqual(hidden_row["reactions"], [{"emoji": "🔥", "count": 1, "slugs": [hidden.slug or ""]}])

    def test_conversation_reactions_mask_hidden_reactor_slug_per_viewer(self) -> None:
        """The inbox last-message payload must not reintroduce raw reactor slugs."""
        hidden = _profile()
        _open_dms(self.creator, hidden)
        add_group_members(self.group, self.creator, [hidden])
        Profile.objects.filter(pk=hidden.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        hidden.refresh_from_db()
        hidden.ensure_slug()
        hidden_auth = _bearer(_token_for(hidden.user))

        message = self._message(body="last reaction")
        react_url = reverse(
            "external_api:messages.groups.messages.react",
            kwargs={"group_uuid": self.group.uuid, "message_id": message.pk},
        )
        self._post_json(react_url, {"emoji": "🔥"}, hidden_auth)

        creator_payload = self.client.get(reverse("external_api:messages.conversations"), **self.creator_auth).json()
        hidden_payload = self.client.get(reverse("external_api:messages.conversations"), **hidden_auth).json()
        creator_row = next(
            item
            for item in creator_payload["results"]
            if item["kind"] == "group" and item["group_uuid"] == str(self.group.uuid)
        )
        hidden_row = next(
            item
            for item in hidden_payload["results"]
            if item["kind"] == "group" and item["group_uuid"] == str(self.group.uuid)
        )
        self.assertEqual(creator_row["last_message"]["reactions"], [{"emoji": "🔥", "count": 1, "slugs": [""]}])
        self.assertEqual(
            hidden_row["last_message"]["reactions"], [{"emoji": "🔥", "count": 1, "slugs": [hidden.slug or ""]}]
        )
