"""Tests for the external API's friend mute surface.

Mute was previously written *over* ``Friendship.status``, which meant muting an
accepted friend un-friended them for every visibility gate reading
``Profile.are_friends``, and left no way back - the pre-mute status was gone and
``FriendshipStatus.can_request`` refuses ``Muted``, so the website's own Unmute
button answered 400. It is now a separate boolean.

These tests pin the API half of that fix: ``is_muted`` is on the wire, the write
is an explicit target rather than a toggle, and - the assertion that matters
most - muting does not disturb ``status``.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class FriendMuteApiTests(TestCase):
    """PATCH /friends/{uuid}/mute/ with an explicit target state."""

    def setUp(self) -> None:
        """Create an accepted friendship and a social-scoped key for its owner."""
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.friend_user = baker.make(User, username="friend")
        self.friend = Profile.objects.get(user=self.friend_user)

        self.friendship = Friendship.objects.create(
            from_profile=self.profile, to_profile=self.friend, status=FriendshipStatus.ACCEPTED
        )

        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.SOCIAL_READ.value, ApiKeyScope.SOCIAL_WRITE.value]
        )

    def _url(self) -> str:
        """The mute endpoint for the fixture's friend."""
        return reverse("external_api:friends.mute", args=[self.friend.uuid])

    def _patch(self, is_muted: bool, raw_key: str | None = None):
        """PATCH the mute state with the fixture's bearer key."""
        return self.client.patch(
            self._url(),
            data={"is_muted": is_muted},
            content_type="application/json",
            **_bearer(raw_key or self.raw_key),
        )

    def test_muting_reports_is_muted_on_the_wire(self) -> None:
        """The app expected this field all along; it was simply never served."""
        response = self._patch(True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_muted"])

    def test_muting_leaves_status_untouched(self) -> None:
        """The whole point of the flag: a muted friend is still a friend."""
        self._patch(True)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.profile))
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)
        # A staticmethod taking both profiles, not a bound instance method -
        # this is the gate that mute-as-a-status used to silently break.
        self.assertTrue(Profile.are_friends(self.profile, self.friend))

    def test_unmuting_is_reachable_and_restores_nothing_else(self) -> None:
        """Unmute was previously impossible - there was no prior state to restore."""
        self._patch(True)

        response = self._patch(False)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_muted"])
        self.friendship.refresh_from_db()
        self.assertFalse(self.friendship.is_muted_by(self.profile))
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)

    def test_the_write_is_an_explicit_target_not_a_toggle(self) -> None:
        """A retried request over a flaky link must not invert the state it set."""
        self._patch(True)
        self._patch(True)
        self._patch(True)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.profile))

    def test_a_body_without_is_muted_is_a_400(self) -> None:
        """Refuse rather than guess - guessing is how a toggle sneaks back in."""
        response = self.client.patch(self._url(), data={}, content_type="application/json", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 400)

    def test_an_unknown_profile_is_a_404(self) -> None:
        """Unknown uuid and no-relationship must be indistinguishable."""
        stranger_user = baker.make(User, username="stranger")
        stranger = Profile.objects.get(user=stranger_user)

        response = self.client.patch(
            reverse("external_api:friends.mute", args=[stranger.uuid]),
            data={"is_muted": True},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 404)

    def test_the_deprecated_post_alias_still_mutes(self) -> None:
        """It shipped first and an integration already calls it."""
        response = self.client.post(self._url(), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.profile))

    def test_a_read_only_credential_cannot_mute(self) -> None:
        """social:read must not imply social:write."""
        api_key, raw = generate_api_key(self.user, "ReadOnly")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.SOCIAL_READ.value])

        response = self._patch(True, raw_key=raw)

        self.assertEqual(response.status_code, 403)
        self.friendship.refresh_from_db()
        self.assertFalse(self.friendship.is_muted_by(self.profile))
