"""Tests for services.identity_visibility and its trip/group-chat wiring.

A trip or group chat can include people who aren't friends with everyone
else in it - if their profile_visibility setting doesn't permit a given
viewer to see their identity, that viewer must still see the trip
activity/comment/message content, but the author's/member's name, username,
and avatar must be masked. Also covers the "suggest connecting" feature:
adding someone unconnected to existing members softly introduces them
(never auto-friends), gated on both sides' allow_friend_recommendations.
"""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.services.connections import recommendable_strangers, suggest_mutual_connection
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identities, resolve_visible_identity


def _profile(*, visibility: str = VisibilityChoice.ANYTHING_IN_COMMON, allow_recs: bool = True) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(
        profile_visibility=visibility,
        direct_message_visibility=VisibilityChoice.ANYONE,
        allow_friend_recommendations=allow_recs,
    )
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


def _make_trip(creator: Profile, **kwargs) -> Trip:
    trip = Trip.objects.create(name="Test Trip", creator=creator, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator, defaults={"rsvp": "yes"})
    return trip


class ResolveVisibleIdentityTests(TestCase):
    """Pure logic: masking, placeholder, and profile-link suppression."""

    def setUp(self) -> None:
        self.viewer = _profile()
        self.friend = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=self.friend, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        self.stranger = _profile(visibility=VisibilityChoice.NO_ONE)

    def test_visible_subject_returns_real_identity(self) -> None:
        identity = resolve_visible_identity(self.viewer, self.friend)
        self.assertFalse(identity["is_masked"])
        self.assertEqual(identity["display_name"], self.friend.username)
        self.assertIsNotNone(identity["display_profile_url"])

    def test_masked_subject_returns_placeholder_and_no_link(self) -> None:
        identity = resolve_visible_identity(self.viewer, self.stranger)
        self.assertTrue(identity["is_masked"])
        self.assertEqual(identity["display_name"], "Member")
        self.assertIsNone(identity["display_avatar_url"])
        self.assertIsNone(identity["display_profile_url"])

    def test_custom_placeholder(self) -> None:
        identity = resolve_visible_identity(self.viewer, self.stranger, placeholder="Former contact")
        self.assertEqual(identity["display_name"], "Former contact")

    def test_viewer_always_sees_their_own_identity(self) -> None:
        identity = resolve_visible_identity(self.stranger, self.stranger)
        self.assertFalse(identity["is_masked"])


class ResolveVisibleIdentitiesTests(TestCase):
    """List resolution: ordinal numbering and distinct colors for masked entries."""

    def setUp(self) -> None:
        self.viewer = _profile()

    def test_multiple_masked_subjects_get_distinct_ordinals(self) -> None:
        hidden_a = _profile(visibility=VisibilityChoice.NO_ONE)
        hidden_b = _profile(visibility=VisibilityChoice.NO_ONE)
        identities = resolve_visible_identities(self.viewer, [hidden_a, hidden_b])
        names = {identities[hidden_a.pk]["display_name"], identities[hidden_b.pk]["display_name"]}
        self.assertEqual(names, {"Member 1", "Member 2"})

    def test_masked_subjects_get_distinct_avatar_colors(self) -> None:
        hidden_a = _profile(visibility=VisibilityChoice.NO_ONE)
        hidden_b = _profile(visibility=VisibilityChoice.NO_ONE)
        identities = resolve_visible_identities(self.viewer, [hidden_a, hidden_b])
        self.assertNotEqual(identities[hidden_a.pk]["avatar_color_class"], identities[hidden_b.pk]["avatar_color_class"])

    def test_mutates_subjects_in_place_for_shared_object_access(self) -> None:
        hidden = _profile(visibility=VisibilityChoice.NO_ONE)
        resolve_visible_identities(self.viewer, [hidden])
        self.assertTrue(hidden.is_masked)
        self.assertEqual(hidden.display_name, "Member 1")


class TripMemberPanelPrivacyTests(TestCase):
    """Trip member list masks a member's identity per their own privacy setting."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.trip = _make_trip(self.creator, allow_add_members=Trip.PERM_EVERYONE)

    def _url(self) -> str:
        return reverse("trips.members", kwargs={"trip_slug": self.trip.slug})

    def test_private_member_identity_is_masked_to_others(self) -> None:
        hidden_member = _profile(visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden_member)

        self.client.force_login(self.creator.user)
        response = self.client.get(self._url())

        self.assertNotContains(response, hidden_member.username)
        self.assertContains(response, "Member")

    def test_friend_identity_is_not_masked(self) -> None:
        friend_member = _profile()
        Friendship.objects.create(from_profile=self.creator, to_profile=friend_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        TripMembership.objects.create(trip=self.trip, profile=friend_member)

        self.client.force_login(self.creator.user)
        response = self.client.get(self._url())

        self.assertContains(response, friend_member.username)


class TripActivityAttributionPrivacyTests(TestCase):
    """Trip activity "Added by" attribution masks the adder's identity when hidden."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.trip = _make_trip(self.creator, allow_add_activities=Trip.PERM_EVERYONE)

    def _url(self) -> str:
        return reverse("trips.activities", kwargs={"trip_slug": self.trip.slug})

    def test_hidden_adder_shows_placeholder_not_username(self) -> None:
        hidden_adder = _profile(visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden_adder)
        TripActivity.objects.create(trip=self.trip, title="Explore the mill", added_by=hidden_adder)

        self.client.force_login(self.creator.user)
        response = self.client.get(self._url())

        self.assertContains(response, "Added by Member")
        self.assertNotContains(response, hidden_adder.username)


class TripCommentAuthorPrivacyTests(TestCase):
    """Trip comment author identity is masked when hidden - comment text still shows."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.trip = _make_trip(self.creator)

    def _url(self) -> str:
        return reverse("trips.comments", kwargs={"trip_slug": self.trip.slug})

    def test_hidden_author_masked_but_comment_text_visible(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        hidden_author = _profile(visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden_author)
        TripComment.objects.create(trip=self.trip, author=hidden_author, text="Great spot, found a secret room!")

        self.client.force_login(self.creator.user)
        response = self.client.get(self._url())

        self.assertContains(response, "Great spot, found a secret room!")
        self.assertNotContains(response, hidden_author.username)
        self.assertContains(response, "Member")


class TripCommentVisibilityGateTests(TestCase):
    """The author's comment_visibility gates the whole trip comment (all-or-nothing),
    matching pin/wiki comments - distinct from profile_visibility, which only
    masks the author's identity while keeping the content visible."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.trip = _make_trip(self.creator)
        self.author = _profile()
        TripMembership.objects.create(trip=self.trip, profile=self.author)

    def _comment(self, text: str = "Meet at the loading dock."):
        from urbanlens.dashboard.models.trips.model import TripComment

        return TripComment.objects.create(trip=self.trip, author=self.author, text=text)

    def _set_comment_visibility(self, value: str) -> None:
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=value)

    def _panel(self):
        return self.client.get(reverse("trips.comments", kwargs={"trip_slug": self.trip.slug}))

    def test_comment_hidden_when_authors_comment_visibility_excludes_viewer(self) -> None:
        self._comment()
        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self.client.force_login(self.creator.user)
        self.assertNotContains(self._panel(), "Meet at the loading dock.")

    def test_comment_visible_when_authors_comment_visibility_permits(self) -> None:
        self._comment()
        self._set_comment_visibility(VisibilityChoice.ANYONE)
        self.client.force_login(self.creator.user)
        self.assertContains(self._panel(), "Meet at the loading dock.")

    def test_author_always_sees_their_own_comment(self) -> None:
        self._comment()
        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self.client.force_login(self.author.user)
        self.assertContains(self._panel(), "Meet at the loading dock.")

    def test_reply_from_hidden_author_is_gated_independently(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        visible = TripComment.objects.create(trip=self.trip, author=self.creator, text="Anyone been inside?")
        TripComment.objects.create(trip=self.trip, author=self.author, parent=visible, text="Yes, last spring.")
        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self.client.force_login(self.creator.user)
        response = self._panel()
        self.assertContains(response, "Anyone been inside?")
        self.assertNotContains(response, "Yes, last spring.")

    def test_reaction_endpoint_404s_for_a_gated_comment(self) -> None:
        comment = self._comment()
        self._set_comment_visibility(VisibilityChoice.NO_ONE)
        self.client.force_login(self.creator.user)
        response = self.client.post(
            reverse("trips.comment.react", kwargs={"trip_slug": self.trip.slug, "comment_id": comment.pk}),
            {"emoji": "\U0001f44d"},
        )
        self.assertEqual(response.status_code, 404)


class LiveMessagePayloadMaskingTests(TestCase):
    """WebSocket message payloads resolve the sender's identity per recipient.

    docs/PROBLEMS.md (PR #111 deferred item, decision 2026-07-23): the
    broadcast payload used to be built once and delivered identically to every
    member, so a live incoming message revealed a raw sender name that a page
    refresh would mask. Payloads are now built per recipient.
    """

    def setUp(self) -> None:
        super().setUp()
        self.viewer = _profile()
        self.hidden_sender = _profile(visibility=VisibilityChoice.NO_ONE)

    def test_group_payload_masks_a_hidden_sender_for_another_member(self) -> None:
        from urbanlens.dashboard.services.group_chats import create_group_chat, create_group_message, serialize_group_message

        group = create_group_chat(self.viewer, "Crew", [self.hidden_sender])
        message = create_group_message(self.hidden_sender, group, "Meet at the gate")

        payload = serialize_group_message(message, viewer=self.viewer)

        self.assertNotEqual(payload["sender_name"], self.hidden_sender.username)
        self.assertEqual(payload["body"], "Meet at the gate")

    def test_group_payload_keeps_the_senders_own_name_for_their_sessions(self) -> None:
        from urbanlens.dashboard.services.group_chats import create_group_chat, create_group_message, serialize_group_message

        group = create_group_chat(self.viewer, "Crew", [self.hidden_sender])
        message = create_group_message(self.hidden_sender, group, "Meet at the gate")

        payload = serialize_group_message(message, viewer=self.hidden_sender)

        self.assertEqual(payload["sender_name"], self.hidden_sender.username)

    def test_group_payload_keeps_a_visible_senders_name(self) -> None:
        from urbanlens.dashboard.services.group_chats import create_group_chat, create_group_message, serialize_group_message

        visible = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=visible, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        group = create_group_chat(self.viewer, "Crew", [visible])
        message = create_group_message(visible, group, "Heading out")

        payload = serialize_group_message(message, viewer=self.viewer)

        self.assertEqual(payload["sender_name"], visible.username)

    def test_direct_message_payload_masks_a_hidden_sender_for_the_recipient(self) -> None:
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage
        from urbanlens.dashboard.services.direct_messages import display_identity_for, serialize_direct_message

        message = DirectMessage.objects.create(sender=self.hidden_sender, recipient=self.viewer, body="hi")

        payload = serialize_direct_message(message, viewer=self.viewer)

        expected = display_identity_for(self.viewer, self.hidden_sender)["display_name"]
        self.assertEqual(payload["sender_name"], expected)
        self.assertNotEqual(payload["sender_name"], self.hidden_sender.username)

    def test_direct_message_payload_without_viewer_keeps_raw_names(self) -> None:
        """The sender's own copy (viewer=None) is the self-view - unmasked."""
        from urbanlens.dashboard.models.direct_messages.model import DirectMessage
        from urbanlens.dashboard.services.direct_messages import serialize_direct_message

        message = DirectMessage.objects.create(sender=self.hidden_sender, recipient=self.viewer, body="hi")

        payload = serialize_direct_message(message)

        self.assertEqual(payload["sender_name"], self.hidden_sender.username)


class GroupMembersDialogPrivacyTests(TestCase):
    """Group members dialog masks a member's identity per their own privacy setting."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.group_chats import create_group_chat

        self.creator = _profile()
        self.visible_member = _profile()
        Friendship.objects.create(from_profile=self.creator, to_profile=self.visible_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        self.group = create_group_chat(self.creator, "Crew", [self.visible_member])

    def test_hidden_member_added_later_is_masked(self) -> None:
        from urbanlens.dashboard.services.group_chats import add_group_members

        hidden_member = _profile(visibility=VisibilityChoice.NO_ONE)
        add_group_members(self.group, self.creator, [hidden_member])

        self.client.force_login(self.creator.user)
        response = self.client.get(reverse("messages.group.members", kwargs={"group_uuid": self.group.uuid}))

        self.assertNotContains(response, hidden_member.username)
        self.assertContains(response, "Member")
        self.assertContains(response, self.visible_member.username)


class GroupMessageSenderPrivacyTests(TestCase):
    """Group message bubbles mask the sender's identity when hidden - message text still shows."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.group_chats import create_group_chat, create_group_message

        self.creator = _profile()
        self.hidden_sender = _profile(visibility=VisibilityChoice.NO_ONE)
        self.group = create_group_chat(self.creator, "Crew", [self.hidden_sender])
        create_group_message(self.hidden_sender, self.group, "Found something interesting here")

    def test_hidden_sender_masked_but_message_visible(self) -> None:
        self.client.force_login(self.creator.user)
        response = self.client.get(reverse("messages.group", kwargs={"group_uuid": self.group.uuid}), HTTP_HX_REQUEST="true")

        self.assertContains(response, "Found something interesting here")
        self.assertNotContains(response, self.hidden_sender.username)


class TripListCardPrivacyTests(TestCase):
    """Trip list cards mask member avatars/creator badge per profile_visibility.

    docs/PROBLEMS.md gap: the single-trip render sites (member panel, activity/
    comment attribution) were already masked, but the trips LIST wasn't - every
    card shows its own member avatars and creator badge, diffuse across
    however many trips are listed at once (see _apply_trip_list_identity_masking).
    """

    def setUp(self) -> None:
        super().setUp()
        self.viewer = _profile()

    def test_hidden_member_avatar_is_masked_on_the_list(self) -> None:
        creator = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=creator, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        trip = _make_trip(creator)
        hidden_member = _profile(visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=trip, profile=hidden_member)

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("trips.list"))

        self.assertNotContains(response, hidden_member.username)

    def test_hidden_creator_badge_is_masked_on_the_list(self) -> None:
        hidden_creator = _profile(visibility=VisibilityChoice.NO_ONE)
        trip = _make_trip(hidden_creator)
        TripMembership.objects.create(trip=trip, profile=self.viewer)

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("trips.list"))

        self.assertNotContains(response, hidden_creator.username)
        self.assertContains(response, "Member")

    def test_visible_member_still_shows_their_username(self) -> None:
        creator = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=creator, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        trip = _make_trip(creator)
        visible_member = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=visible_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        TripMembership.objects.create(trip=trip, profile=visible_member)

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("trips.list"))

        self.assertContains(response, visible_member.username)


class PinWikiCommentAuthorPrivacyTests(TestCase):
    """Pin/wiki comment author identity is masked per profile_visibility - comment text still shows.

    docs/PROBLEMS.md gap: the comment's content was already all-or-nothing
    gated by can_view_comments_from (comment_visibility, a different field) -
    but once a comment passed that gate, its author's own name/avatar weren't
    separately masked per profile_visibility.
    """

    def setUp(self) -> None:
        super().setUp()
        self.viewer = _profile()

    def test_hidden_wiki_comment_author_is_masked(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = Location.objects.create(latitude=41.0, longitude=-74.0)
        wiki = Wiki.objects.create(location=location, name="Old Mill Wiki")
        baker.make(Pin, profile=self.viewer, location=location)
        hidden_author = _profile(visibility=VisibilityChoice.NO_ONE)
        Comment.objects.create(wiki=wiki, profile=hidden_author, text="Watch the third floor.")

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("location.wiki.comments", args=[location.slug]))

        self.assertContains(response, "Watch the third floor.")
        self.assertNotContains(response, hidden_author.username)
        self.assertContains(response, "Member")

    def test_visible_wiki_comment_author_still_links_to_their_profile(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = Location.objects.create(latitude=42.0, longitude=-75.0)
        wiki = Wiki.objects.create(location=location, name="Old Factory Wiki")
        baker.make(Pin, profile=self.viewer, location=location)
        visible_author = _profile()
        Friendship.objects.create(from_profile=self.viewer, to_profile=visible_author, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        Comment.objects.create(wiki=wiki, profile=visible_author, text="Great find!")

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("location.wiki.comments", args=[location.slug]))

        self.assertContains(response, visible_author.username)
        self.assertContains(response, reverse("profile.view_user", kwargs={"profile_slug": visible_author.slug}))


class TripInviteNotificationPrivacyTests(TestCase):
    """Trip-invite notification text masks the inviter's identity when hidden.

    docs/PROBLEMS.md gap: notification text is baked in as a plain-text
    string at creation time, so a template-side fix can't reach it later -
    it must be resolved (and masked if needed) before formatting.
    """

    def test_added_to_trip_notification_masks_a_hidden_inviter(self) -> None:
        inviter = _profile(visibility=VisibilityChoice.NO_ONE)
        invitee = _profile()
        trip = _make_trip(inviter, allow_add_members=Trip.PERM_EVERYONE)
        self.client.force_login(inviter.user)

        response = self.client.post(
            reverse("trips.members", kwargs={"trip_slug": trip.slug}),
            data=json.dumps({"username": invitee.username}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        notification = NotificationLog.objects.get(profile=invitee, notification_type=NotificationType.ADDED_TO_TRIP)
        self.assertNotIn(inviter.username, notification.message)
        self.assertIn("Member", notification.message)

    def test_trip_invite_via_dm_masks_a_hidden_sender(self) -> None:
        from urbanlens.dashboard.services.direct_message_shares import invite_to_trip_in_message

        sender = _profile(visibility=VisibilityChoice.NO_ONE)
        recipient = _profile()
        # NO_ONE excludes even accepted friends (see VisibilityChoice's docstring) -
        # being connected doesn't guarantee sender is visible to recipient.
        Friendship.objects.create(from_profile=sender, to_profile=recipient, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        trip = _make_trip(sender)

        invite_to_trip_in_message(sender, recipient, trip, "Join my trip!")

        notification = NotificationLog.objects.get(profile=recipient, notification_type=NotificationType.ADDED_TO_TRIP)
        self.assertNotIn(sender.username, notification.message)
        self.assertIn("Member", notification.message)


class GroupAddNotificationTextPrivacyTests(TestCase):
    """"X added you to the group" notification text masks the actor's identity
    when hidden - _notify_group_event stores it as a plain-text NotificationLog
    (notification_type=MESSAGE, title=group.name)."""

    def test_group_creation_notification_masks_a_hidden_creator(self) -> None:
        from urbanlens.dashboard.services.group_chats import create_group_chat

        hidden_creator = _profile(visibility=VisibilityChoice.NO_ONE)
        member = _profile()
        Friendship.objects.create(from_profile=hidden_creator, to_profile=member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)

        group = create_group_chat(hidden_creator, "Crew", [member])

        notification = NotificationLog.objects.get(profile=member, notification_type=NotificationType.MESSAGE, title=group.name)
        self.assertNotIn(hidden_creator.username, notification.message)
        self.assertIn("Member", notification.message)

    def test_add_group_members_notification_masks_a_hidden_actor(self) -> None:
        from urbanlens.dashboard.services.group_chats import add_group_members, create_group_chat

        hidden_actor = _profile(visibility=VisibilityChoice.NO_ONE)
        existing_member = _profile()
        Friendship.objects.create(from_profile=hidden_actor, to_profile=existing_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        group = create_group_chat(hidden_actor, "Crew", [existing_member])

        new_member = _profile()
        Friendship.objects.create(from_profile=hidden_actor, to_profile=new_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        add_group_members(group, hidden_actor, [new_member])

        notification = NotificationLog.objects.get(profile=new_member, notification_type=NotificationType.MESSAGE, title=group.name)
        self.assertNotIn(hidden_actor.username, notification.message)
        self.assertIn("Member", notification.message)


class DirectMessageThreadPartnerMaskingTests(TestCase):
    """_thread.html's block-confirm/empty-state/composer text masks a hidden partner.

    docs/PROBLEMS.md gap: the thread header already used display_name/
    display_avatar_url via display_identity_for, but four other spots in the
    same template still used raw partner.username.
    """

    def setUp(self) -> None:
        super().setUp()
        self.viewer = _profile()

    def _thread_url(self, partner: Profile) -> str:
        return reverse("messages.conversation", kwargs={"profile_slug": partner.slug})

    def test_empty_state_and_composer_mask_a_hidden_partner(self) -> None:
        # direct_message_visibility (set to ANYONE by the _profile() helper) is
        # what governs whether this conversation can even be opened - a
        # separate field from profile_visibility, which is what must be masked.
        hidden_partner = _profile(visibility=VisibilityChoice.NO_ONE)

        self.client.force_login(self.viewer.user)
        response = self.client.get(self._thread_url(hidden_partner), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(hidden_partner.username, content)
        self.assertIn("Former contact", content)


class RecommendableStrangersTests(TestCase):
    """recommendable_strangers() gates the friend-suggestion feature."""

    def setUp(self) -> None:
        self.new_member = _profile()
        self.other = _profile()

    def test_unconnected_opted_in_pair_is_recommendable(self) -> None:
        self.assertEqual(recommendable_strangers(self.new_member, [self.other]), [self.other])

    def test_already_connected_pair_is_not_recommendable(self) -> None:
        Friendship.objects.create(from_profile=self.new_member, to_profile=self.other, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        self.assertEqual(recommendable_strangers(self.new_member, [self.other]), [])

    def test_new_member_opted_out_disables_all_suggestions(self) -> None:
        Profile.objects.filter(pk=self.new_member.pk).update(allow_friend_recommendations=False)
        self.new_member.refresh_from_db()
        self.assertEqual(recommendable_strangers(self.new_member, [self.other]), [])

    def test_other_opted_out_excludes_just_that_pair(self) -> None:
        Profile.objects.filter(pk=self.other.pk).update(allow_friend_recommendations=False)
        self.other.refresh_from_db()
        self.assertEqual(recommendable_strangers(self.new_member, [self.other]), [])

    def test_blocked_pair_is_not_recommendable(self) -> None:
        Friendship.objects.create(from_profile=self.other, to_profile=self.new_member, status=FriendshipStatus.BLOCKED)
        self.assertEqual(recommendable_strangers(self.new_member, [self.other]), [])

    def test_self_is_never_recommended(self) -> None:
        self.assertEqual(recommendable_strangers(self.new_member, [self.new_member]), [])


class SuggestMutualConnectionTests(TestCase):
    """suggest_mutual_connection() sends notifications, never a friend request or profile-view bypass."""

    def setUp(self) -> None:
        self.a = _profile()
        self.b = _profile()

    def test_sends_notification_to_both(self) -> None:
        suggest_mutual_connection(self.a, self.b)
        self.assertTrue(NotificationLog.objects.filter(profile=self.a, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=self.b, notification_type=NotificationType.FRIEND_SUGGESTION).exists())

    def test_never_creates_a_friend_request(self) -> None:
        suggest_mutual_connection(self.a, self.b)
        self.assertFalse(Friendship.objects.filter(from_profile__in=[self.a, self.b], to_profile__in=[self.a, self.b]).exists())

    def test_does_not_grant_profile_view_access(self) -> None:
        """A masked (e.g. NO_ONE-visibility) profile must not become viewable just
        because they were suggested as a connection - allow_friend_recommendations
        is about recommending someone to others, not unlocking their own profile."""
        suggest_mutual_connection(self.a, self.b)
        self.assertFalse(DirectMessageTemporaryAccess.objects.filter(profile=self.a, granted_to=self.b).exists())
        self.assertFalse(DirectMessageTemporaryAccess.objects.filter(profile=self.b, granted_to=self.a).exists())

    def test_notification_message_masks_a_hidden_subject(self) -> None:
        Profile.objects.filter(pk=self.b.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        self.b.refresh_from_db()
        suggest_mutual_connection(self.a, self.b)
        notification_to_a = NotificationLog.objects.get(profile=self.a, notification_type=NotificationType.FRIEND_SUGGESTION)
        self.assertNotIn(self.b.username, notification_to_a.message)
        self.assertIn("Member", notification_to_a.message)


class TripAddMemberSuggestsConnectionTests(TestCase):
    """Adding an unconnected member to a trip triggers a soft connection suggestion."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.trip = _make_trip(self.creator, allow_add_members=Trip.PERM_EVERYONE)
        self.client.force_login(self.creator.user)

    def _url(self) -> str:
        return reverse("trips.members", kwargs={"trip_slug": self.trip.slug})

    def test_adding_unconnected_member_suggests_connection_with_creator(self) -> None:
        new_user = baker.make("auth.User", username="newmember")
        Profile.objects.filter(user=new_user).update(allow_friend_recommendations=True)
        response = self.client.post(self._url(), data=json.dumps({"username": "newmember"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        new_profile = Profile.objects.get(user=new_user)
        self.assertTrue(NotificationLog.objects.filter(profile=self.creator, source_profile=new_profile, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=new_profile, source_profile=self.creator, notification_type=NotificationType.FRIEND_SUGGESTION).exists())

    def test_opted_out_new_member_gets_no_suggestion(self) -> None:
        new_user = baker.make("auth.User", username="newmember")
        Profile.objects.filter(user=new_user).update(allow_friend_recommendations=False)
        self.client.post(self._url(), data=json.dumps({"username": "newmember"}), content_type="application/json")

        new_profile = Profile.objects.get(user=new_user)
        self.assertFalse(NotificationLog.objects.filter(notification_type=NotificationType.FRIEND_SUGGESTION, profile__in=[self.creator, new_profile]).exists())

    def test_already_connected_new_member_gets_no_suggestion(self) -> None:
        new_user = baker.make("auth.User", username="newmember")
        new_profile = Profile.objects.get(user=new_user)
        Profile.objects.filter(pk=new_profile.pk).update(allow_friend_recommendations=True)
        Friendship.objects.create(from_profile=self.creator, to_profile=new_profile, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)

        self.client.post(self._url(), data=json.dumps({"username": "newmember"}), content_type="application/json")

        self.assertFalse(NotificationLog.objects.filter(notification_type=NotificationType.FRIEND_SUGGESTION).exists())


class GroupAddMemberSuggestsConnectionTests(TestCase):
    """Adding an unconnected member to a group chat triggers a soft connection suggestion."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.group_chats import create_group_chat

        self.creator = _profile()
        self.other_member = _profile()
        Friendship.objects.create(from_profile=self.creator, to_profile=self.other_member, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND, permissions=Permission.VIEW_PROFILE)
        self.group = create_group_chat(self.creator, "Crew", [self.other_member])

    def test_adding_unconnected_member_suggests_with_existing_members(self) -> None:
        from urbanlens.dashboard.services.group_chats import add_group_members

        new_member = _profile()
        add_group_members(self.group, self.creator, [new_member])

        self.assertTrue(NotificationLog.objects.filter(profile=new_member, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=self.creator, source_profile=new_member, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=self.other_member, source_profile=new_member, notification_type=NotificationType.FRIEND_SUGGESTION).exists())

    def test_group_creation_suggests_connections_among_unconnected_initial_members(self) -> None:
        from urbanlens.dashboard.services.group_chats import create_group_chat

        member_a = _profile()
        member_b = _profile()
        creator = _profile()
        create_group_chat(creator, "New Crew", [member_a, member_b])

        # member_a <-> member_b weren't connected to each other or the creator.
        self.assertTrue(NotificationLog.objects.filter(profile=member_a, source_profile=member_b, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
        self.assertTrue(NotificationLog.objects.filter(profile=member_a, source_profile=creator, notification_type=NotificationType.FRIEND_SUGGESTION).exists())
