"""Tests that an actual Friendship.BLOCKED row is enforced as an absolute veto."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.direct_messages import can_direct_message, create_direct_message


def _profile(**kwargs) -> Profile:
    return baker.make("auth.User", **kwargs).profile


def _set_dm_visibility(profile: Profile, visibility: str) -> None:
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=visibility)
    profile.refresh_from_db()


def _block(blocker: Profile, target: Profile) -> Friendship:
    return Friendship.objects.create(from_profile=blocker, to_profile=target, status=FriendshipStatus.BLOCKED)


class AreBlockedTests(TestCase):
    """Profile.are_blocked checks both directions of a BLOCKED Friendship row."""

    def test_no_relationship_is_not_blocked(self) -> None:
        a, b = _profile(), _profile()
        self.assertFalse(Profile.are_blocked(a, b))

    def test_blocker_direction_is_blocked(self) -> None:
        a, b = _profile(), _profile()
        _block(a, b)
        self.assertTrue(Profile.are_blocked(a, b))

    def test_reverse_direction_is_also_blocked(self) -> None:
        """It must not matter which profile is checked as 'subject' - a block
        by either party is a mutual veto."""
        a, b = _profile(), _profile()
        _block(a, b)
        self.assertTrue(Profile.are_blocked(b, a))

    def test_accepted_friendship_is_not_blocked(self) -> None:
        a, b = _profile(), _profile()
        Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)
        self.assertFalse(Profile.are_blocked(a, b))


class BlockedDirectMessageEnforcementTests(TestCase):
    """A BLOCKED Friendship row must stop DMs regardless of visibility settings."""

    def setUp(self) -> None:
        super().setUp()
        self.blocker = _profile()
        self.blocked = _profile()

    def test_blocked_sender_denied_even_with_anyone_visibility(self) -> None:
        """The bug this fixes: ANYONE is the most permissive setting and previously let a blocked user through completely unchecked."""
        _set_dm_visibility(self.blocker, VisibilityChoice.ANYONE)
        _block(self.blocker, self.blocked)
        self.assertFalse(can_direct_message(self.blocked, self.blocker))
        self.assertFalse(self.blocker.accepts_direct_messages_from(self.blocked))

    def test_blocked_sender_denied_under_every_visibility_setting(self) -> None:
        _block(self.blocker, self.blocked)
        for choice in VisibilityChoice.values:
            _set_dm_visibility(self.blocker, choice)
            self.assertFalse(can_direct_message(self.blocked, self.blocker), f"visibility={choice}")

    def test_block_overrides_the_already_messaged_reply_exception(self) -> None:
        """Blocking someone you'd previously messaged must still stop them from replying - the reply exception can't outrank an explicit block."""
        _set_dm_visibility(self.blocker, VisibilityChoice.NO_ONE)
        DirectMessage.objects.create(sender=self.blocker, recipient=self.blocked, body="hi, before I blocked you")
        # Without the block, the reply exception would now permit self.blocked -> self.blocker.
        self.assertTrue(can_direct_message(self.blocked, self.blocker))
        _block(self.blocker, self.blocked)
        self.assertFalse(can_direct_message(self.blocked, self.blocker))

    def test_create_direct_message_raises_permission_error(self) -> None:
        _set_dm_visibility(self.blocker, VisibilityChoice.ANYONE)
        _block(self.blocker, self.blocked)
        with self.assertRaises(PermissionError):
            create_direct_message(self.blocked, self.blocker, "hello")
        self.assertFalse(DirectMessage.objects.exists())

    def test_blocked_target_also_cannot_message_the_blocker(self) -> None:
        """The party who did the blocking is protected in both send directions -
        blocking isn't a one-way mute of incoming messages only."""
        _set_dm_visibility(self.blocked, VisibilityChoice.ANYONE)
        _block(self.blocker, self.blocked)
        self.assertFalse(can_direct_message(self.blocker, self.blocked))


class BlockedSendViewTests(TestCase):
    """POST /messages/<slug>/send/ must 403 once the recipient has blocked the sender."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile(username="sender-of-record")
        self.partner = _profile(username="blocker-of-record")
        self.client.force_login(self.me.user)
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)

    def test_send_succeeds_before_being_blocked(self) -> None:
        response = self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}), {"body": "hello"}
        )
        self.assertEqual(response.status_code, 200)

    def test_send_403s_once_blocked(self) -> None:
        _block(self.partner, self.me)
        response = self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}), {"body": "hello"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DirectMessage.objects.exists())
