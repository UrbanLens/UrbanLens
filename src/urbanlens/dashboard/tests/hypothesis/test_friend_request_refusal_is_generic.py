"""A refused friend request does not say which policy refused it (G3-12)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social.friendship import may_send_friend_request

_REFUSING_POLICIES = [
    VisibilityChoice.NO_ONE,
    VisibilityChoice.FRIENDS,
    VisibilityChoice.COMMON_PIN,
    VisibilityChoice.COMMON_FRIEND,
    VisibilityChoice.COMMON_TRIP,
    VisibilityChoice.ANYTHING_IN_COMMON,
]


class FriendRequestRefusalTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.requester = baker.make(User).profile
        self.target = baker.make(User).profile
        # Visible, so these exercise the policy refusal; a profile the requester cannot see answers like none at all.
        Profile.objects.filter(pk=self.target.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        self.client.force_login(self.requester.user)

    def _refusal_for(self, policy: str, *, htmx: bool = False) -> tuple[int, bytes]:
        Profile.objects.filter(pk=self.target.pk).update(friend_request_visibility=policy)
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        response = self.client.post(reverse("friend.request", kwargs={"profile_id": self.target.pk}), **headers)
        return response.status_code, response.content

    def _nonexistent(self) -> tuple[int, bytes]:
        response = self.client.post(reverse("friend.request", kwargs={"profile_id": 999_999}))
        return response.status_code, response.content

    def test_every_refusing_policy_answers_identically(self) -> None:
        answers = {policy: self._refusal_for(policy) for policy in _REFUSING_POLICIES}

        self.assertEqual(len(set(answers.values())), 1, answers)
        self.assertEqual(next(iter(answers.values()))[0], 403)
        self.assertFalse(Friendship.objects.exists())

    def test_htmx_refusals_are_identical_too(self) -> None:
        answers = {self._refusal_for(policy, htmx=True) for policy in _REFUSING_POLICIES}

        self.assertEqual(len(answers), 1)

    def test_a_permitted_request_still_goes_through(self) -> None:
        """Negative control: the gate must still admit."""
        status, _body = self._refusal_for(VisibilityChoice.ANYONE)

        self.assertEqual(status, 302)
        self.assertTrue(Friendship.objects.filter(from_profile=self.requester, to_profile=self.target).exists())

    def test_the_web_and_the_api_share_one_gate(self) -> None:
        """Community off on either side refuses on both surfaces."""
        Profile.objects.filter(pk=self.target.pk).update(
            friend_request_visibility=VisibilityChoice.ANYONE, community_enabled=False
        )
        self.target.refresh_from_db()

        self.assertFalse(may_send_friend_request(self.requester, self.target))
        status, _body = self._refusal_for(VisibilityChoice.ANYONE)
        self.assertEqual((status, _body), self._refusal_for(VisibilityChoice.NO_ONE))

    def test_being_blocked_answers_like_no_profile_at_all(self) -> None:
        """A profile that blocked the requester reads as nonexistent, as it does on every other route."""
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType

        Friendship.objects.create(
            from_profile=self.target,
            to_profile=self.requester,
            status=FriendshipStatus.BLOCKED,
            relationship_type=FriendshipType.FRIEND,
        )

        self.assertEqual(self._refusal_for(VisibilityChoice.ANYONE), self._nonexistent())
        self.assertFalse(may_send_friend_request(self.requester, self.target))

    def test_a_hidden_profile_that_refuses_answers_like_no_profile_at_all(self) -> None:
        Profile.objects.filter(pk=self.target.pk).update(profile_visibility=VisibilityChoice.NO_ONE)

        for policy in _REFUSING_POLICIES:
            with self.subTest(policy):
                self.assertEqual(self._refusal_for(policy), self._nonexistent())
        self.assertFalse(Friendship.objects.exists())

    def test_requesting_yourself_is_refused(self) -> None:
        self.assertFalse(may_send_friend_request(self.requester, self.requester))
