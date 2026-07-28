"""External API: friends list, friend-request transitions, and scope enforcement.

The transition tests assert the *exact* wire value of ``FriendshipStatus``,
which is capitalized ("Accepted", not "accepted"). Asserting merely that it
isn't lowercase would not have caught the real bug this guards against, which
was a serializer normalizing the value on the way out.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for the test client.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, *scopes: ApiKeyScope) -> str:
    """Issue an API key granting exactly ``scopes``.

    Newly issued keys deliberately do not carry the social/notification
    scopes (see ``_default_api_key_scopes``), so every test that needs one
    must opt in explicitly - which is itself the behaviour being relied on.

    Args:
        user: The key's owner.
        scopes: The scopes to grant.

    Returns:
        The plaintext key.
    """
    api_key, raw_key = generate_api_key(user, "Test")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


class SocialScopeEnforcementTests(TestCase):
    """Every social endpoint must fail closed without its scope."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other = Profile.objects.get(user=baker.make(User))

    def test_default_api_key_does_not_grant_social_scopes(self) -> None:
        """A freshly issued key must not reach the social surface at all.

        This is the cross-domain "no widening, no backfill" rule: adding
        social scopes to the default grant would silently hand every
        already-issued integration access to the owner's friends list.
        """
        _api_key, raw_key = generate_api_key(self.user, "Zapier")
        response = self.client.get(reverse("external_api:friends"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_friends_list_requires_social_read(self) -> None:
        raw_key = _key_with_scopes(self.user, ApiKeyScope.PROFILE_READ)
        response = self.client.get(reverse("external_api:friends"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)

    def test_friends_list_allowed_with_social_read(self) -> None:
        raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ)
        response = self.client.get(reverse("external_api:friends"), **_bearer(raw_key))
        self.assertEqual(response.status_code, 200)

    def test_social_read_does_not_grant_writes(self) -> None:
        """A read-only key must not be able to mutate a relationship."""
        raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ)
        for name in ("accept", "reject", "ignore", "block", "mute"):
            with self.subTest(action=name):
                url = reverse(f"external_api:friends.{name}", kwargs={"profile_uuid": self.other.uuid})
                response = self.client.post(url, **_bearer(raw_key))
                self.assertEqual(response.status_code, 403)

    def test_friend_invite_requires_social_write(self) -> None:
        raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ)
        response = self.client.post(
            reverse("external_api:friend_invites"),
            {"email": "someone@example.com"},
            content_type="application/json",
            **_bearer(raw_key),
        )
        self.assertEqual(response.status_code, 403)


class FriendshipTransitionMatrixTests(TestCase):
    """Each transition endpoint must leave the exact capitalized status behind."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.requester = Profile.objects.get(user=baker.make(User))
        self.raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)

    def _pending_request(self) -> Friendship:
        """Create a pending request from ``requester`` to the calling profile."""
        return Friendship.objects.create(
            from_profile=self.requester,
            to_profile=self.profile,
            status=FriendshipStatus.REQUESTED,
            relationship_type="Friend",
        )

    def test_accept_sets_literal_accepted(self) -> None:
        friendship = self._pending_request()
        url = reverse("external_api:friends.accept", kwargs={"profile_uuid": self.requester.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, "Accepted")
        self.assertEqual(response.json()["status"], "Accepted")

    def test_reject_sets_literal_declined(self) -> None:
        friendship = self._pending_request()
        url = reverse("external_api:friends.reject", kwargs={"profile_uuid": self.requester.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, "Declined")
        self.assertEqual(response.json()["status"], "Declined")

    def test_ignore_sets_literal_ignored_and_blocks_re_request(self) -> None:
        """Ignored is a distinct, permanent state - not a synonym for Declined."""
        friendship = self._pending_request()
        url = reverse("external_api:friends.ignore", kwargs={"profile_uuid": self.requester.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, "Ignored")
        # Declined/Removed permit a re-request; Ignored deliberately does not.
        self.assertFalse(FriendshipStatus.can_request(friendship.status))

    def test_block_sets_literal_blocked_without_existing_row(self) -> None:
        """Blocking must work against a stranger - that is what it is for."""
        stranger = Profile.objects.get(user=baker.make(User))
        url = reverse("external_api:friends.block", kwargs={"profile_uuid": stranger.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        friendship = Friendship.objects.all().between(self.profile, stranger)
        self.assertIsNotNone(friendship)
        self.assertEqual(friendship.status, "Blocked")

    def test_mute_sets_the_flag_and_keeps_the_friendship(self) -> None:
        """Mute is the one action here that is NOT a status transition.

        It used to write ``status="Muted"``, which un-friended the pair for
        every gate that reads ``Profile.are_friends`` - so an API client that
        muted a friend silently revoked their own access. The endpoint now
        moves ``Friendship.muted`` and leaves the relationship intact.
        """
        friendship = self._pending_request()
        friendship.status = FriendshipStatus.ACCEPTED
        friendship.save()
        url = reverse("external_api:friends.mute", kwargs={"profile_uuid": self.requester.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertTrue(friendship.muted)
        self.assertEqual(friendship.status, "Accepted")
        self.assertEqual(response.json()["status"], "Accepted")

    def test_remove_sets_literal_removed(self) -> None:
        friendship = self._pending_request()
        friendship.status = FriendshipStatus.ACCEPTED
        friendship.save()
        url = reverse("external_api:friends.detail", kwargs={"profile_uuid": self.requester.uuid})

        response = self.client.delete(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 204)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, "Removed")

    def test_every_status_wire_value_is_capitalized(self) -> None:
        """Guards the whole enum, not just the values the endpoints above set."""
        expected = {"Pending", "Requested", "Accepted", "Declined", "Removed", "Muted", "Blocked", "Ignored"}
        self.assertEqual({value for value, _label in FriendshipStatus.choices}, expected)

    def test_mute_on_stranger_is_404(self) -> None:
        """Unlike block, mute needs an existing relationship."""
        stranger = Profile.objects.get(user=baker.make(User))
        url = reverse("external_api:friends.mute", kwargs={"profile_uuid": stranger.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 404)

    def test_unknown_profile_uuid_is_404(self) -> None:
        from uuid import uuid4

        url = reverse("external_api:friends.accept", kwargs={"profile_uuid": uuid4()})
        response = self.client.post(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)


class FriendRequestGateTests(TestCase):
    """POST friends/ must refuse with a 404 rather than a distinguishing 403."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.target = Profile.objects.get(user=baker.make(User))
        self.raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)
        self.url = reverse("external_api:friends")

    def _post(self, target: Profile) -> object:
        """Send a friend request to ``target``."""
        return self.client.post(
            self.url,
            {"profile_uuid": str(target.uuid)},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

    def test_visibility_no_one_reads_as_404(self) -> None:
        self.target.friend_request_visibility = VisibilityChoice.NO_ONE
        self.target.save()

        response = self._post(self.target)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Friendship.objects.filter(to_profile=self.target).exists())

    def test_visibility_rejection_is_byte_identical_to_unknown_profile(self) -> None:
        """A refused request must not be distinguishable from a nonexistent one."""
        from uuid import uuid4

        self.target.friend_request_visibility = VisibilityChoice.NO_ONE
        self.target.save()

        refused = self._post(self.target)
        unknown = self.client.post(
            self.url,
            {"profile_uuid": str(uuid4())},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(refused.status_code, unknown.status_code)
        self.assertEqual(refused.content, unknown.content)

    def test_target_with_community_disabled_reads_as_404(self) -> None:
        self.target.community_enabled = False
        self.target.save()

        response = self._post(self.target)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Friendship.objects.filter(to_profile=self.target).exists())

    def test_caller_with_community_disabled_reads_as_404(self) -> None:
        """The veto applies to the sender's own setting too, not just the target's."""
        self.profile.community_enabled = False
        self.profile.save()
        self.target.friend_request_visibility = VisibilityChoice.ANYONE
        self.target.save()

        response = self._post(self.target)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Friendship.objects.filter(to_profile=self.target).exists())

    def test_open_visibility_creates_the_request(self) -> None:
        self.target.friend_request_visibility = VisibilityChoice.ANYONE
        self.target.save()

        response = self._post(self.target)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Requested")
        self.assertEqual(response.json()["direction"], "outgoing")

    def test_cannot_friend_request_yourself(self) -> None:
        response = self._post(self.profile)
        self.assertEqual(response.status_code, 404)


class BlockedProfileVetoTests(TestCase):
    """A BLOCKED relationship vetoes further contact in either direction."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.blocker = Profile.objects.get(user=baker.make(User))
        self.raw_key = _key_with_scopes(self.user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)

    def test_blocked_by_target_cannot_send_request(self) -> None:
        """The blocker blocked us; a new request must not resurrect the row."""
        self.blocker.friend_request_visibility = VisibilityChoice.ANYONE
        self.blocker.save()
        Friendship.objects.create(
            from_profile=self.blocker,
            to_profile=self.profile,
            status=FriendshipStatus.BLOCKED,
        )

        response = self.client.post(
            reverse("external_api:friends"),
            {"profile_uuid": str(self.blocker.uuid)},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 400)
        friendship = Friendship.objects.all().between(self.profile, self.blocker)
        self.assertEqual(friendship.status, "Blocked")

    def test_friends_list_defaults_to_accepted_only(self) -> None:
        Friendship.objects.create(from_profile=self.profile, to_profile=self.blocker, status=FriendshipStatus.BLOCKED)
        friend = Profile.objects.get(user=baker.make(User))
        Friendship.objects.create(from_profile=self.profile, to_profile=friend, status=FriendshipStatus.ACCEPTED)

        response = self.client.get(reverse("external_api:friends"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "Accepted")

    def test_friends_list_can_filter_to_pending_requests(self) -> None:
        Friendship.objects.create(from_profile=self.blocker, to_profile=self.profile, status=FriendshipStatus.REQUESTED)

        response = self.client.get(reverse("external_api:friends"), {"status": "Requested"}, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "Requested")
        self.assertEqual(results[0]["direction"], "incoming")

    def test_friends_list_rejects_a_lowercase_status_filter(self) -> None:
        """The wire vocabulary is capitalized; a lowercase value is not valid."""
        response = self.client.get(reverse("external_api:friends"), {"status": "accepted"}, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_friends_list_never_includes_other_peoples_relationships(self) -> None:
        stranger_a = Profile.objects.get(user=baker.make(User))
        stranger_b = Profile.objects.get(user=baker.make(User))
        Friendship.objects.create(from_profile=stranger_a, to_profile=stranger_b, status=FriendshipStatus.ACCEPTED)

        response = self.client.get(reverse("external_api:friends"), **_bearer(self.raw_key))

        self.assertEqual(response.json()["results"], [])
