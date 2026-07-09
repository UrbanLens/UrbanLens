"""Tests for the Community toggle (Profile.community_enabled).

Covers the three enforcement points: Pin.save() forcing is_private, Profile.save()
forcing the seven VisibilityChoice fields to NO_ONE, and Friendship.request()/
.accept() refusing to create or accept requests for a disabled profile.
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import _COMMUNITY_GATED_VISIBILITY_FIELDS
from urbanlens.dashboard.services.community import bulk_privatize_pins

_hyp = settings(max_examples=25, deadline=None)


class PinPrivacyInvariantTests(TestCase):
    """A pin can never be persisted as non-private while its owner has Community off."""

    def test_pin_forced_private_on_create_when_community_disabled(self) -> None:
        location = baker.make_recipe("dashboard.location")
        profile = baker.make_recipe("dashboard.pin").profile
        profile.community_enabled = False
        profile.save(update_fields=["community_enabled"])

        pin = Pin.objects.create(profile=profile, location=location, is_private=False)

        self.assertTrue(pin.is_private)

    def test_pin_cannot_be_unprivated_while_community_disabled(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin", is_private=True)
        pin.profile.community_enabled = False
        pin.profile.save(update_fields=["community_enabled"])

        pin.is_private = False
        pin.save(update_fields=["is_private"])
        pin.refresh_from_db()

        self.assertTrue(pin.is_private)

    def test_pin_privacy_untouched_when_community_enabled(self) -> None:
        pin: Pin = baker.make_recipe("dashboard.pin", is_private=False)
        self.assertTrue(pin.profile.community_enabled)

        pin.save()
        pin.refresh_from_db()

        self.assertFalse(pin.is_private)

    @given(st.booleans(), st.booleans())
    @_hyp
    def test_invariant_holds_across_create_and_resave(self, community_enabled: bool, requested_is_private: bool) -> None:
        """pin.is_private or profile.community_enabled always holds after Pin.save()."""
        location = baker.make_recipe("dashboard.location")
        profile = baker.make_recipe("dashboard.pin").profile
        profile.community_enabled = community_enabled
        profile.save(update_fields=["community_enabled"])

        pin = Pin.objects.create(profile=profile, location=location, is_private=requested_is_private)
        self.assertTrue(pin.is_private or profile.community_enabled)

        # Re-saving unchanged must not violate the invariant either.
        pin.save()
        pin.refresh_from_db()
        self.assertTrue(pin.is_private or profile.community_enabled)


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


class BulkPrivatizePinsTests(TestCase):
    """bulk_privatize_pins forces every non-private pin for a profile to private, in one pass."""

    def test_privatizes_all_non_private_pins(self) -> None:
        profile = baker.make_recipe("dashboard.pin").profile
        public_pin: Pin = baker.make_recipe("dashboard.pin", profile=profile, is_private=False)
        already_private: Pin = baker.make_recipe("dashboard.pin", profile=profile, is_private=True)
        other_profile_pin: Pin = baker.make_recipe("dashboard.pin", is_private=False)

        count = bulk_privatize_pins(profile)

        public_pin.refresh_from_db()
        already_private.refresh_from_db()
        other_profile_pin.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertTrue(public_pin.is_private)
        self.assertTrue(already_private.is_private)
        self.assertFalse(other_profile_pin.is_private)
