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
