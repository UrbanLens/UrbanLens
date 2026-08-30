"""The notification-dropdown respond endpoint answers only a real incoming request.

``friend.respond`` resolved the pair's Friendship with ``between()``, which
matches in either direction and reports nothing about status, then called
``accept()``/``decline()``, which overwrite status unconditionally. Two things
follow from that pair, and both are checked here: a requester could accept
their own outgoing request, and a blocked party could decline the block away.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile


def _profile(username: str) -> Profile:
    return baker.make("auth.User", username=username).profile


class FriendRespondRequiresIncomingRequestTests(TestCase):
    def setUp(self) -> None:
        self.alice = _profile("alice")
        self.bob = _profile("bob")

    def _respond_as(self, actor: Profile, other: Profile, action: str):
        self.client.force_login(actor.user)
        return self.client.post(reverse("friend.respond", args=[other.pk]), {"action": action})

    def test_requester_cannot_accept_their_own_outgoing_request(self) -> None:
        friendship = Friendship.objects.create(from_profile=self.bob, to_profile=self.alice, status=FriendshipStatus.REQUESTED)

        # Bob sent it, so Bob has nothing to answer - the row must not move.
        response = self._respond_as(self.bob, self.alice, "accept")
        self.assertEqual(response.status_code, 404)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.REQUESTED)

        # Alice, who received it, can.
        response = self._respond_as(self.alice, self.bob, "accept")
        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)

    def test_a_blocked_party_cannot_decline_the_block_away(self) -> None:
        block = Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=FriendshipStatus.BLOCKED)

        response = self._respond_as(self.bob, self.alice, "decline")
        self.assertEqual(response.status_code, 404)
        block.refresh_from_db()
        self.assertEqual(block.status, FriendshipStatus.BLOCKED)
        self.assertTrue(Profile.are_blocked(self.alice, self.bob))

    def test_declining_a_real_incoming_request_still_works(self) -> None:
        """The guard must reject the two cases above without breaking the ordinary one."""
        friendship = Friendship.objects.create(from_profile=self.bob, to_profile=self.alice, status=FriendshipStatus.REQUESTED)

        response = self._respond_as(self.alice, self.bob, "decline")
        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.DECLINED)
