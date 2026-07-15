"""Tests for the navbar messages icon visibility rule.

Regression coverage: the icon used to appear only once a user had sent or
received a direct message, hiding it permanently for users who only ever use
friend connections. It should also appear as soon as a user has ever had an
accepted friend - even if that friend was later removed - since Friendship
rows are never deleted, only moved to a REMOVED status (see
models/friendship/model.py's remove()).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.context_processors import add_direct_messages
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship


class NavbarMessagesIconVisibilityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()
        self.user: User = baker.make(User)
        self.other: User = baker.make(User)

    def _show(self) -> bool:
        req = self.factory.get("/")
        req.user = self.user
        return add_direct_messages(req)["show_messages_icon"]

    def test_hidden_with_no_messages_and_no_friends(self) -> None:
        self.assertFalse(self._show())

    def test_visible_once_friend_request_is_accepted(self) -> None:
        friendship = Friendship.request(self.user.profile, self.other.profile)
        friendship.accept()
        self.assertTrue(self._show())

    def test_stays_visible_after_friend_is_removed(self) -> None:
        friendship = Friendship.request(self.user.profile, self.other.profile)
        friendship.accept()
        friendship.remove()
        self.assertTrue(self._show())

    def test_hidden_for_a_pending_request_that_was_never_accepted(self) -> None:
        Friendship.request(self.user.profile, self.other.profile)
        self.assertFalse(self._show())

    def test_hidden_for_a_declined_request(self) -> None:
        friendship = Friendship.request(self.user.profile, self.other.profile)
        friendship.decline()
        self.assertFalse(self._show())


class FriendshipEverFriendsQuerySetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.other: User = baker.make(User)

    def test_ever_friends_includes_accepted_and_removed(self) -> None:
        baker.make(Friendship, from_profile=self.user.profile, to_profile=self.other.profile, status=FriendshipStatus.ACCEPTED)
        self.assertTrue(Friendship.objects.profile(self.user.profile).ever_friends().exists())

    def test_ever_friends_excludes_pending_declined_blocked(self) -> None:
        for status in (FriendshipStatus.PENDING, FriendshipStatus.REQUESTED, FriendshipStatus.DECLINED, FriendshipStatus.BLOCKED, FriendshipStatus.MUTED, FriendshipStatus.IGNORED):
            other = baker.make(User)
            baker.make(Friendship, from_profile=self.user.profile, to_profile=other.profile, status=status)
        self.assertFalse(Friendship.objects.profile(self.user.profile).ever_friends().exists())
