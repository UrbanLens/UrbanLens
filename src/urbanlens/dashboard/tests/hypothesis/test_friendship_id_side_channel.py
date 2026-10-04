"""P281: the friendship routes that take a profile id cost the same, and answer the same, for a hidden account as for none.

They looked the id up, then decided in Python whether the actor could know of the account. An id nobody holds stopped
after one statement and a hidden account's ran the rest, so walking ids told the actor which accounts are hidden from
it rather than deleted.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.tests.hypothesis.test_request_body_profile_side_channel import _BodySideChannelCase

#: The routes acting on a relationship, which all answer a profile the actor cannot know of as a missing one.
_ACTIONS = (
    "friend.accept",
    "friend.reject",
    "friend.ignore",
    "friend.remove",
    "friend.block",
    "friend.unblock",
    "friend.mute",
    "friend.unmute",
)


class FriendshipIdRouteTests(_BodySideChannelCase):
    def _post(
        self, name: str, probe: str, payload: dict | None = None, *, kwarg: str = "profile_id"
    ) -> tuple[int, str]:
        return self._web(reverse(name, kwargs={kwarg: int(probe)}), payload or {})

    def test_requesting_a_friend(self) -> None:
        self.assert_id_indistinguishable(lambda probe: self._post("friend.request", probe, {"message": ""}))

    def test_acting_on_a_relationship(self) -> None:
        for name in _ACTIONS:
            with self.subTest(route=name):
                self.assert_id_indistinguishable(lambda probe, name=name: self._post(name, probe))

    def test_answering_a_request_from_the_notification_menu(self) -> None:
        self.assert_id_indistinguishable(
            lambda probe: self._post("friend.respond", probe, {"action": "accept"}, kwarg="from_profile_id")
        )

    def test_reading_a_friend_list(self) -> None:
        def read(probe: str) -> tuple[int, str]:
            response = self.client.get(reverse("friend.list", kwargs={"profile_id": int(probe)}))
            return response.status_code, response.content.decode()

        self.assert_id_indistinguishable(read)

    def test_opening_someone_else_s_friends_page(self) -> None:
        for name in ("friend.page", "friend.page_widget"):

            def read(probe: str, name: str = name) -> tuple[int, str]:
                response = self.client.get(reverse(name, kwargs={"profile_id": int(probe)}))
                return response.status_code, response.content.decode()

            with self.subTest(route=name):
                self.assert_id_indistinguishable(read)


class FriendshipIdRoutesStillWorkTests(_BodySideChannelCase):
    """Anti-vacuity: each route still reaches an account the actor knows of."""

    def test_an_open_account_can_be_sent_a_request(self) -> None:
        self.client.post(reverse("friend.request", kwargs={"profile_id": self.visible.pk}), {"message": ""})

        self.assertTrue(
            Friendship.objects.filter(
                from_profile=self.viewer, to_profile=self.visible, status=FriendshipStatus.REQUESTED
            ).exists()
        )

    def test_a_request_can_be_accepted_from_the_notification_menu(self) -> None:
        asker = baker.make(User, username="eager_asker").profile
        Friendship.objects.create(from_profile=asker, to_profile=self.viewer, status=FriendshipStatus.REQUESTED)

        self.client.post(reverse("friend.respond", kwargs={"from_profile_id": asker.pk}), {"action": "accept"})

        self.assertTrue(Profile.are_friends(self.viewer, asker))

    def test_a_visible_account_s_friend_list_renders(self) -> None:
        response = self.client.get(reverse("friend.list", kwargs={"profile_id": self.visible.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.strip())

    def test_a_visible_stranger_can_be_blocked(self) -> None:
        self.client.post(reverse("friend.block", kwargs={"profile_id": self.visible.pk}))

        self.assertTrue(self.viewer.has_blocked(self.visible))

    def test_your_own_friends_page_opens(self) -> None:
        response = self.client.get(reverse("friend.page", kwargs={"profile_id": self.viewer.pk}))

        self.assertEqual(response.status_code, 200)
