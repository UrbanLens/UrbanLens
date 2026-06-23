"""Additional FriendshipQuerySet tests for methods not covered by test_friendship.py.

test_friendship.py already covers: between, is_friend, not_friend, profile.
This file adds: user, status, relationship_type, has_permission.
"""
from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.friendship.meta import (
    FriendshipStatus,
    FriendshipType,
    Permission,
)
from urbanlens.dashboard.models.friendship.model import Friendship


_DEFAULTS = dict(
    relationship_type=FriendshipType.FRIEND,
    permissions=Permission.VIEW_PROFILE,
)


def _make_friendship(from_profile, to_profile, **kwargs) -> Friendship:
    params = {**_DEFAULTS, "status": FriendshipStatus.REQUESTED, **kwargs}
    return Friendship.objects.create(
        from_profile=from_profile,
        to_profile=to_profile,
        **params,
    )


# ── user() ────────────────────────────────────────────────────────────────────

class FriendshipQuerySetUserTests(TestCase):
    """user() returns friendships where the user is on either side."""

    def setUp(self):
        self.user_a = baker.make("auth.User")
        self.user_b = baker.make("auth.User")
        self.user_c = baker.make("auth.User")
        self.f_ab = _make_friendship(self.user_a.profile, self.user_b.profile)

    def test_returns_friendship_for_initiating_user(self) -> None:
        qs = Friendship.objects.all().user(self.user_a)
        self.assertIn(self.f_ab, qs)

    def test_returns_friendship_for_receiving_user(self) -> None:
        qs = Friendship.objects.all().user(self.user_b)
        self.assertIn(self.f_ab, qs)

    def test_excludes_unrelated_user(self) -> None:
        qs = Friendship.objects.all().user(self.user_c)
        self.assertNotIn(self.f_ab, qs)


# ── status() ─────────────────────────────────────────────────────────────────

class FriendshipQuerySetStatusTests(TestCase):
    """status() filters by the named status value."""

    def setUp(self):
        self.profile_a = baker.make("auth.User").profile
        self.profile_b = baker.make("auth.User").profile
        self.profile_c = baker.make("auth.User").profile
        self.f_requested = _make_friendship(
            self.profile_a, self.profile_b, status=FriendshipStatus.REQUESTED
        )
        self.f_accepted = _make_friendship(
            self.profile_a, self.profile_c, status=FriendshipStatus.ACCEPTED
        )

    def test_status_requested_includes_requested_friendship(self) -> None:
        qs = Friendship.objects.all().status(FriendshipStatus.REQUESTED)
        self.assertIn(self.f_requested, qs)
        self.assertNotIn(self.f_accepted, qs)

    def test_status_accepted_includes_accepted_friendship(self) -> None:
        qs = Friendship.objects.all().status(FriendshipStatus.ACCEPTED)
        self.assertIn(self.f_accepted, qs)
        self.assertNotIn(self.f_requested, qs)


# ── relationship_type() ───────────────────────────────────────────────────────

class FriendshipQuerySetRelationshipTypeTests(TestCase):
    """relationship_type() filters by the named relationship type."""

    def setUp(self):
        self.profile_a = baker.make("auth.User").profile
        self.profile_b = baker.make("auth.User").profile
        self.profile_c = baker.make("auth.User").profile
        self.f_friend = _make_friendship(
            self.profile_a, self.profile_b,
            relationship_type=FriendshipType.FRIEND,
        )
        self.f_connected = _make_friendship(
            self.profile_a, self.profile_c,
            relationship_type=FriendshipType.CONNECTED,
        )

    def test_friend_type_includes_friend_friendship(self) -> None:
        qs = Friendship.objects.all().relationship_type(FriendshipType.FRIEND)
        self.assertIn(self.f_friend, qs)
        self.assertNotIn(self.f_connected, qs)

    def test_connected_type_includes_connected_friendship(self) -> None:
        qs = Friendship.objects.all().relationship_type(FriendshipType.CONNECTED)
        self.assertIn(self.f_connected, qs)
        self.assertNotIn(self.f_friend, qs)


# ── has_permission() ──────────────────────────────────────────────────────────

class FriendshipQuerySetHasPermissionTests(TestCase):
    """has_permission() filters friendships by the permissions field."""

    def setUp(self):
        self.profile_a = baker.make("auth.User").profile
        self.profile_b = baker.make("auth.User").profile
        self.profile_c = baker.make("auth.User").profile
        self.f_view = _make_friendship(
            self.profile_a, self.profile_b,
            permissions=Permission.VIEW_PROFILE,
        )
        self.f_share = _make_friendship(
            self.profile_a, self.profile_c,
            permissions=Permission.SHARE_LOCATIONS,
        )

    def test_view_profile_permission_included(self) -> None:
        qs = Friendship.objects.all().has_permission(Permission.VIEW_PROFILE)
        self.assertIn(self.f_view, qs)

    def test_share_locations_permission_excluded_from_view_filter(self) -> None:
        qs = Friendship.objects.all().has_permission(Permission.VIEW_PROFILE)
        self.assertNotIn(self.f_share, qs)

    def test_share_locations_permission_included_in_own_filter(self) -> None:
        qs = Friendship.objects.all().has_permission(Permission.SHARE_LOCATIONS)
        self.assertIn(self.f_share, qs)
