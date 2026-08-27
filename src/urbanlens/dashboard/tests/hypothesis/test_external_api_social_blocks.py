"""Blocking, unblocking, and the one thing neither may ever do: be reversible by the wrong person.

A block is a safety feature on a site whose users meet strangers at derelict
buildings. Every test here exists because the block was, in one way or another,
clearable by the person it was placed on:

* ``services.social.friendship._existing_friendship`` resolves the row with
  ``Friendship.objects.between(...)``, which matches **either direction**, and
  ``remove_friend`` applied ``Friendship.remove()`` to whatever came back. So
  ``DELETE /friends/{blocker_uuid}/`` with ``social:write`` let the blocked
  party set the row to ``Removed`` and then re-contact the person who blocked
  them. The site's own ``friend.remove`` button had the same hole.
* There was no unblock path at all - ``block_profile`` had no inverse - so the
  profile page's "Unblock" button posted to ``friend.remove``, i.e. straight
  into the defect above.
* ``Friendship`` stores no "who blocked whom" column, and ``block_profile``
  reused whichever row already joined the pair. A block placed on an inbound
  friend request therefore left ``from_profile`` pointing at the *blocked*
  party, so direction could not be trusted to identify the blocker until
  ``block_profile`` started normalizing it.

Every refusal here asserts the stored row as well as the status code. A 404
that still mutated the row would be strictly worse than the original bug: the
attacker gets what they wanted *and* the audit trail says they were refused.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


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


class _BlockTestCase(TestCase):
    """Two accounts, each holding a full-social API key."""

    def setUp(self) -> None:
        """Create a blocker and a target, each with social read+write keys."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.blocker_user = baker.make(User, username="blocker")
        self.blocker = Profile.objects.get(user=self.blocker_user)
        self.blocked_user = baker.make(User, username="blocked")
        self.blocked = Profile.objects.get(user=self.blocked_user)

        self.blocker_key = _key_with_scopes(self.blocker_user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)
        self.blocked_key = _key_with_scopes(self.blocked_user, ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)

    def _block(self) -> Friendship:
        """Have ``blocker`` block ``blocked`` through the real endpoint.

        Going through the endpoint rather than ``Friendship.objects.create``
        is deliberate: the row's *direction* is what identifies the blocker,
        and only the service normalizes it.

        Returns:
            The stored relationship row.
        """
        response = self.client.post(
            reverse("external_api:friends.block", kwargs={"profile_uuid": self.blocked.uuid}),
            **_bearer(self.blocker_key),
        )
        self.assertEqual(response.status_code, 200)
        friendship = Friendship.objects.all().between(self.blocker, self.blocked)
        assert friendship is not None
        return friendship

    def _assert_still_blocked(self, friendship: Friendship) -> None:
        """Assert the row is untouched: still blocked, still owned by the blocker.

        Args:
            friendship: The row captured before the refused request.
        """
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.BLOCKED)
        self.assertEqual(friendship.from_profile_id, self.blocker.pk)
        self.assertEqual(friendship.to_profile_id, self.blocked.pk)


class BlockedPartyCannotClearTheBlockTests(_BlockTestCase):
    """The P0: a blocked user must not be able to remove the block on them."""

    def test_blocked_party_delete_is_404_and_leaves_the_row_alone(self) -> None:
        """``DELETE /friends/{blocker}/`` from the blocked party changes nothing."""
        friendship = self._block()

        response = self.client.delete(
            reverse("external_api:friends.detail", kwargs={"profile_uuid": self.blocker.uuid}),
            **_bearer(self.blocked_key),
        )

        self.assertEqual(response.status_code, 404)
        self._assert_still_blocked(friendship)

    def test_blocked_party_unblock_is_404_and_leaves_the_row_alone(self) -> None:
        """The unblock endpoint is not a second way in for the blocked party."""
        friendship = self._block()

        response = self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": self.blocker.uuid}),
            **_bearer(self.blocked_key),
        )

        self.assertEqual(response.status_code, 404)
        self._assert_still_blocked(friendship)

    def test_block_placed_on_an_inbound_request_still_belongs_to_the_blocker(self) -> None:
        """Blocking a pending requester must not leave the requester owning the row.

        ``block_profile`` reuses the existing relationship row, and the row
        created by a friend request has ``from_profile`` = the *requester*. If
        that direction survived the block, the blocked party would read as the
        blocker and could clear their own block.
        """
        Friendship.objects.create(
            from_profile=self.blocked,
            to_profile=self.blocker,
            status=FriendshipStatus.REQUESTED,
        )

        friendship = self._block()

        self.assertEqual(friendship.from_profile_id, self.blocker.pk)
        self.assertEqual(friendship.to_profile_id, self.blocked.pk)

        response = self.client.delete(
            reverse("external_api:friends.detail", kwargs={"profile_uuid": self.blocker.uuid}),
            **_bearer(self.blocked_key),
        )
        self.assertEqual(response.status_code, 404)
        self._assert_still_blocked(friendship)

    def test_website_remove_button_cannot_clear_someone_elses_block(self) -> None:
        """The same hole existed on the site itself, not only on the API."""
        friendship = self._block()
        self.client.force_login(self.blocked_user)

        response = self.client.post(reverse("friend.remove", args=[self.blocker.pk]))

        self.assertEqual(response.status_code, 404)
        self._assert_still_blocked(friendship)


class UnblockTests(_BlockTestCase):
    """The inverse of block, which did not exist before."""

    def test_blocker_can_unblock(self) -> None:
        """The person who placed the block can lift it, leaving ``Removed``."""
        friendship = self._block()

        response = self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": self.blocked.uuid}),
            **_bearer(self.blocker_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], FriendshipStatus.REMOVED)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.REMOVED)

    def test_unblock_permits_a_fresh_request_afterwards(self) -> None:
        """``Removed`` is the state ``can_request`` allows, so contact can resume."""
        self._block()
        self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": self.blocked.uuid}),
            **_bearer(self.blocker_key),
        )
        friendship = Friendship.objects.all().between(self.blocker, self.blocked)
        assert friendship is not None
        self.assertTrue(FriendshipStatus.can_request(friendship.status))

    def test_website_unblock_button_lifts_the_block(self) -> None:
        """The profile page's Unblock button reaches the real unblock action."""
        friendship = self._block()
        self.client.force_login(self.blocker_user)

        response = self.client.post(reverse("friend.unblock", args=[self.blocked.pk]))

        self.assertIn(response.status_code, (200, 302))
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.REMOVED)

    def test_unblock_requires_social_write(self) -> None:
        """A read-only credential cannot lift a block."""
        self._block()
        read_only = _key_with_scopes(self.blocker_user, ApiKeyScope.SOCIAL_READ)

        response = self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": self.blocked.uuid}),
            **_bearer(read_only),
        )

        self.assertEqual(response.status_code, 403)


class UnblockRefusalsAreIndistinguishableTests(_BlockTestCase):
    """Unknown uuid, no row, and a row blocked by someone else all answer alike."""

    def _unblock(self, profile_uuid) -> object:
        """POST the unblock endpoint as the blocker.

        Args:
            profile_uuid: The uuid named in the path.

        Returns:
            The test-client response.
        """
        return self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": profile_uuid}),
            **_bearer(self.blocker_key),
        )

    def test_unknown_uuid_is_404(self) -> None:
        """A uuid belonging to nobody is a plain 404."""
        response = self._unblock(uuid4())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such profile."})

    def test_no_relationship_row_is_the_same_404(self) -> None:
        """A real stranger answers byte-identically to a nonexistent uuid."""
        stranger = Profile.objects.get(user=baker.make(User))

        unknown = self._unblock(uuid4())
        no_row = self._unblock(stranger.uuid)

        self.assertEqual(no_row.status_code, unknown.status_code)
        self.assertEqual(no_row.content, unknown.content)

    def test_row_that_is_not_blocked_is_the_same_404(self) -> None:
        """An accepted friendship is not unblockable, and says nothing else."""
        friend = Profile.objects.get(user=baker.make(User))
        friendship = Friendship.objects.create(from_profile=self.blocker, to_profile=friend, status=FriendshipStatus.ACCEPTED)

        unknown = self._unblock(uuid4())
        not_blocked = self._unblock(friend.uuid)

        self.assertEqual(not_blocked.status_code, unknown.status_code)
        self.assertEqual(not_blocked.content, unknown.content)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)

    def test_block_placed_by_someone_else_is_the_same_404(self) -> None:
        """Being *inside* a block tells you nothing about it through this endpoint."""
        self._block()

        unknown = self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": uuid4()}),
            **_bearer(self.blocked_key),
        )
        someone_elses = self.client.post(
            reverse("external_api:friends.unblock", kwargs={"profile_uuid": self.blocker.uuid}),
            **_bearer(self.blocked_key),
        )

        self.assertEqual(someone_elses.status_code, unknown.status_code)
        self.assertEqual(someone_elses.content, unknown.content)
