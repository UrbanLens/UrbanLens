"""Tests for the Community toggle (Profile.community_enabled).

Covers the two enforcement points: Profile.save() forcing the seven
VisibilityChoice fields to NO_ONE, and Friendship.request()/.accept() refusing
to create or accept requests for a disabled profile. (Pin.is_private is gone:
wikis are user-created only, so pins carry no privacy flag any more.)
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS


class ProfileVisibilityForcingTests(TestCase):
    """Profile.save() forces every gated VisibilityChoice field to NO_ONE while Community is off."""

    def test_visibility_fields_forced_to_no_one_when_community_off(self) -> None:
        profile = baker.make_recipe("dashboard.pin").profile
        for field in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            setattr(profile, field, VisibilityChoice.ANYONE)
        profile.community_enabled = False
        profile.save()
        profile.refresh_from_db()

        for field in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            self.assertEqual(getattr(profile, field), VisibilityChoice.NO_ONE, f"{field} was not forced to NO_ONE")

    def test_visibility_fields_untouched_when_community_on(self) -> None:
        profile = baker.make_recipe("dashboard.pin").profile
        for field in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            setattr(profile, field, VisibilityChoice.FRIENDS)
        profile.save()
        profile.refresh_from_db()

        for field in _COMMUNITY_GATED_VISIBILITY_FIELDS:
            self.assertEqual(getattr(profile, field), VisibilityChoice.FRIENDS)

    def test_reenabling_community_leaves_fields_at_no_one(self) -> None:
        """Per design: no snapshot/restore - re-enabling just makes fields editable again."""
        profile = baker.make_recipe("dashboard.pin").profile
        profile.profile_visibility = VisibilityChoice.ANYONE
        profile.community_enabled = False
        profile.save()

        profile.community_enabled = True
        profile.save()
        profile.refresh_from_db()

        self.assertEqual(profile.profile_visibility, VisibilityChoice.NO_ONE)


class FriendshipCommunityBlockTests(TestCase):
    """Friendship.request()/.accept() refuse to act when either side has Community off."""

    def test_request_blocked_when_sender_community_disabled(self) -> None:
        sender = baker.make_recipe("dashboard.pin").profile
        sender.community_enabled = False
        sender.save(update_fields=["community_enabled"])
        recipient = baker.make_recipe("dashboard.pin").profile

        result = Friendship.request(from_profile=sender, to_profile=recipient)

        self.assertIsNone(result)
        self.assertFalse(Friendship.objects.filter(from_profile=sender, to_profile=recipient).exists())

    def test_request_blocked_when_recipient_community_disabled(self) -> None:
        sender = baker.make_recipe("dashboard.pin").profile
        recipient = baker.make_recipe("dashboard.pin").profile
        recipient.community_enabled = False
        recipient.save(update_fields=["community_enabled"])

        result = Friendship.request(from_profile=sender, to_profile=recipient)

        self.assertIsNone(result)

    def test_request_succeeds_when_both_community_enabled(self) -> None:
        sender = baker.make_recipe("dashboard.pin").profile
        recipient = baker.make_recipe("dashboard.pin").profile

        result = Friendship.request(from_profile=sender, to_profile=recipient)

        self.assertIsNotNone(result)

    def test_accept_blocked_when_either_side_community_disabled(self) -> None:
        friendship: Friendship = baker.make_recipe("dashboard.friendship", status=FriendshipStatus.REQUESTED)
        friendship.to_profile.community_enabled = False
        friendship.to_profile.save(update_fields=["community_enabled"])

        accepted = friendship.accept()

        self.assertFalse(accepted)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.REQUESTED)

    def test_accept_succeeds_when_both_community_enabled(self) -> None:
        friendship: Friendship = baker.make_recipe("dashboard.friendship", status=FriendshipStatus.REQUESTED)

        accepted = friendship.accept()

        self.assertTrue(accepted)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)
