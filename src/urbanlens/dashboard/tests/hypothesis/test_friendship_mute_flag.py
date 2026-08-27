"""Muting a friend must not un-friend them.

Mute used to be implemented as a ``FriendshipStatus`` value, which meant the
single ``status`` column had to encode two genuinely independent facts: *what
kind of relationship is this* and *do I want notifications from it*. Writing
``Muted`` into that column destroyed the first fact to record the second, and
the consequences were not cosmetic:

- ``Profile.are_friends`` matches ``status == ACCEPTED`` only, so the moment
  you muted a friend the pair stopped being friends for **every** downstream
  visibility gate - profile fields, pin visibility, direct-message permission,
  friend-request evaluation, common-pin/common-trip queries. Muting someone
  silently revoked their access to you and yours to them.
- ``FriendshipStatus.can_request`` excludes ``Muted``, so the profile page's
  own "Unmute" button (which posted to ``friend.request``) could never
  succeed - ``Friendship.request`` refused, the controller answered 400, and
  the relationship was stuck at ``Muted`` with no way back short of a DB edit.
- The prior status was not recorded anywhere, so even a hand-written unmute
  had nothing to restore.

The fix is a mute flag stored separately from ``status``, one column per side
of the relationship. These tests pin the resulting invariant: **mute and unmute
change one person's flag and nothing else**, for every status a relationship
can be in. What the flag then *suppresses* is pinned in
``test_friendship_mute_suppression.py``.
"""

from __future__ import annotations

import importlib

from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social.friendship import FriendshipNotFoundError, mute_profile, unmute_profile

_db_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.function_scoped_fixture],
)

#: Every status a stored relationship can legitimately sit in. ``Muted`` is
#: deliberately absent: it is the legacy value this change retires, and no code
#: path writes it any more.
_LIVE_STATUSES = [
    FriendshipStatus.PENDING,
    FriendshipStatus.REQUESTED,
    FriendshipStatus.ACCEPTED,
    FriendshipStatus.DECLINED,
    FriendshipStatus.REMOVED,
    FriendshipStatus.BLOCKED,
    FriendshipStatus.IGNORED,
]


def _profile(**kwargs) -> Profile:
    """Create a profile via its auto-created user.

    Args:
        **kwargs: Passed through to the ``auth.User`` baker call.

    Returns:
        The new profile.
    """
    return baker.make("auth.User", **kwargs).profile


def _friendship(from_profile: Profile, to_profile: Profile, status: str = FriendshipStatus.ACCEPTED) -> Friendship:
    """Create one relationship row directly, bypassing the transition methods.

    Args:
        from_profile: The originating profile.
        to_profile: The other profile.
        status: The status to store.

    Returns:
        The new Friendship.
    """
    return Friendship.objects.create(
        from_profile=from_profile,
        to_profile=to_profile,
        status=status,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class MuteFlagDoesNotClobberStatusTests(TestCase):
    """``mute``/``unmute`` move the flag and leave ``status`` untouched."""

    def setUp(self) -> None:
        """Create two profiles with an accepted friendship between them."""
        super().setUp()
        self.actor = _profile()
        self.other_profile = _profile()
        self.friendship = _friendship(self.actor, self.other_profile)

    def test_muting_a_friend_leaves_them_a_friend(self) -> None:
        """The regression this whole change exists for.

        ``Profile.are_friends`` is the chokepoint every visibility gate reads;
        if muting flips it to False, muting silently revokes access.
        """
        mute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)
        self.assertTrue(self.friendship.is_muted_by(self.actor))
        self.assertTrue(Profile.are_friends(self.actor, self.other_profile))

    def test_unmute_clears_the_flag_and_still_leaves_status_alone(self) -> None:
        """Unmuting is reachable and is a pure inverse of muting."""
        mute_profile(self.actor, self.other_profile)
        unmute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)
        self.assertFalse(self.friendship.is_muted_by(self.actor))
        self.assertTrue(Profile.are_friends(self.actor, self.other_profile))

    def test_mute_is_idempotent(self) -> None:
        """A retried mute (flaky mobile link, double-tap) must be a no-op."""
        mute_profile(self.actor, self.other_profile)
        mute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.actor))
        self.assertEqual(Friendship.objects.all().profile(self.actor).count(), 1)

    def test_unmute_is_idempotent(self) -> None:
        """Unmuting an already-unmuted relationship is not an error."""
        unmute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertFalse(self.friendship.is_muted_by(self.actor))

    def test_mute_works_in_either_direction(self) -> None:
        """The row is directional; the action is not - either party may mute."""
        mute_profile(self.other_profile, self.actor)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.other_profile))
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)

    def test_muting_is_not_mutual(self) -> None:
        """The bug that made wiring mute into delivery unsafe until now.

        One row joins the pair, so a single shared boolean meant A muting B
        also read as muted from B's side - and B, who asked for nothing, would
        have been the one silenced.
        """
        mute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.actor))
        self.assertFalse(self.friendship.is_muted_by(self.other_profile))

    def test_one_sides_unmute_leaves_the_others_mute_alone(self) -> None:
        """Both may mute independently; neither clears the other."""
        mute_profile(self.actor, self.other_profile)
        mute_profile(self.other_profile, self.actor)

        unmute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertFalse(self.friendship.is_muted_by(self.actor))
        self.assertTrue(self.friendship.is_muted_by(self.other_profile))

    def test_a_stranger_has_no_side_of_the_row_to_read(self) -> None:
        """Answering False would be a guess; every wrong guess silences somebody."""
        with self.assertRaises(ValueError):
            self.friendship.is_muted_by(_profile())

    def test_mute_without_a_relationship_raises_not_found(self) -> None:
        """Mute is a volume control on an existing relationship, not a veto on a stranger."""
        stranger = _profile()
        with self.assertRaises(FriendshipNotFoundError):
            mute_profile(self.actor, stranger)

    def test_unmute_without_a_relationship_raises_not_found(self) -> None:
        """Same 404-shaped failure as mute, so the pair stay symmetric."""
        stranger = _profile()
        with self.assertRaises(FriendshipNotFoundError):
            unmute_profile(self.actor, stranger)

    def test_default_is_unmuted(self) -> None:
        """A brand-new relationship is not muted, by either party."""
        self.assertFalse(self.friendship.is_muted_by(self.actor))
        self.assertFalse(self.friendship.is_muted_by(self.other_profile))

    @given(status=st.sampled_from(_LIVE_STATUSES))
    @_db_settings
    def test_mute_never_rewrites_any_status(self, status: str) -> None:
        """Property: whatever status a row holds, muting preserves it exactly."""
        Friendship.objects.filter(pk=self.friendship.pk).update(status=status, muted_by_from_profile=False, muted_by_to_profile=False)

        mute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, status)
        self.assertTrue(self.friendship.is_muted_by(self.actor))

    @given(status=st.sampled_from(_LIVE_STATUSES))
    @_db_settings
    def test_unmute_never_rewrites_any_status(self, status: str) -> None:
        """Property: the inverse holds too - unmute is status-preserving."""
        Friendship.objects.filter(pk=self.friendship.pk).update(status=status, muted_by_from_profile=True, muted_by_to_profile=True)

        unmute_profile(self.actor, self.other_profile)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, status)
        self.assertFalse(self.friendship.is_muted_by(self.actor))
        self.assertTrue(self.friendship.is_muted_by(self.other_profile), "unmuting is one person's decision")


class MuteQuerySetTests(TestCase):
    """``muted_by()``/``not_muted_by()`` read the flags, never the status."""

    def setUp(self) -> None:
        """Create one muted and one unmuted accepted friendship."""
        super().setUp()
        self.actor = _profile()
        self.muted_friend = _profile()
        self.loud_friend = _profile()
        self.muted_row = _friendship(self.actor, self.muted_friend)
        self.loud_row = _friendship(self.actor, self.loud_friend)
        self.muted_row.mute(self.actor)

    def test_muted_filter_returns_only_muted_rows(self) -> None:
        pks = set(Friendship.objects.all().muted_by(self.actor).values_list("pk", flat=True))
        self.assertEqual(pks, {self.muted_row.pk})

    def test_unmuted_filter_returns_only_unmuted_rows(self) -> None:
        pks = set(Friendship.objects.all().not_muted_by(self.actor).values_list("pk", flat=True))
        self.assertEqual(pks, {self.loud_row.pk})

    def test_the_other_partys_mute_is_not_the_viewers(self) -> None:
        """The filter answers "rows I muted", so the far side's flag must not leak in."""
        self.loud_row.mute(self.loud_friend)

        self.assertEqual(set(Friendship.objects.all().muted_by(self.actor).values_list("pk", flat=True)), {self.muted_row.pk})
        self.assertEqual(set(Friendship.objects.all().muted_by(self.loud_friend).values_list("pk", flat=True)), {self.loud_row.pk})

    def test_muted_rows_are_still_friends(self) -> None:
        """The whole point: muted rows stay inside ``is_friend()``."""
        pks = set(Friendship.objects.all().profile(self.actor).is_friend().values_list("pk", flat=True))
        self.assertEqual(pks, {self.muted_row.pk, self.loud_row.pk})


class LegacyMutedRowRepairWiringTests(SimpleTestCase):
    """``0010_v0_6_0``'s legacy ``status='Muted'`` repair, checked structurally.

    These used to run the migration's two ``(apps, schema_editor)`` callables
    against the *live* app registry, which was legitimate only while the
    historical ``Friendship`` at 0010 was field-identical to the current one.
    Migration ``0057`` split ``muted`` into one column per side, so the
    callables now reference a column the live schema no longer has and can only
    be executed against a real historical state.

    What is still worth holding, and does not need a database, is that the
    migration wires both directions to the real functions. The forward pass is
    what un-breaks rows whose relationship state was destroyed by the old
    encoding; a reverse quietly swapped for ``noop`` would leave a rollback
    with ``Accepted`` rows the pre-0010 code reads as un-muted - see
    ``test_migration_noop_reverse_guard``.
    """

    #: Imported by path: the module name starts with a digit.
    migration = importlib.import_module("urbanlens.dashboard.migrations.0010_v0_6_0")

    def test_both_directions_of_the_repair_are_wired(self) -> None:
        operations = [op for op in self.migration.Migration.operations if type(op).__name__ == "RunPython" and op.code is self.migration.restore_muted_friendships]

        self.assertEqual(len(operations), 1)
        self.assertIs(operations[0].reverse_code, self.migration.collapse_muted_flag_into_status)


class MuteWebsiteButtonTests(TestCase):
    """The profile page's Mute/Unmute buttons must both work.

    ``friend.unmute`` did not exist before this change - the template's Unmute
    button posted to ``friend.request``, which ``FriendshipStatus.can_request``
    refuses for ``Muted``, so the button answered 400 every single time.
    """

    def setUp(self) -> None:
        """Log in as the actor and give them one accepted friend."""
        super().setUp()
        self.actor = _profile(username="actor", password="pw")
        self.other_profile = _profile(username="target")
        self.friendship = _friendship(self.actor, self.other_profile)
        self.client.force_login(self.actor.user)

    def test_mute_button_sets_the_flag_without_dropping_the_friendship(self) -> None:
        response = self.client.post(reverse("friend.mute", args=[self.other_profile.pk]), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.actor))
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)

    def test_unmute_button_clears_the_flag(self) -> None:
        self.friendship.mute(self.actor)

        response = self.client.post(reverse("friend.unmute", args=[self.other_profile.pk]), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.friendship.refresh_from_db()
        self.assertFalse(self.friendship.is_muted_by(self.actor))
        self.assertEqual(self.friendship.status, FriendshipStatus.ACCEPTED)

    def test_unmute_of_a_stranger_is_404_not_400(self) -> None:
        """Nothing to unmute reads as "not found", matching every sibling action."""
        stranger = _profile()
        response = self.client.post(reverse("friend.unmute", args=[stranger.pk]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 404)

    def test_unmute_of_a_missing_profile_is_404(self) -> None:
        response = self.client.post(reverse("friend.unmute", args=[9_999_999]), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 404)
