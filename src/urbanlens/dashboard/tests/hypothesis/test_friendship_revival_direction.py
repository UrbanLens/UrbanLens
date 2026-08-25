"""Re-requesting somebody you already had a relationship with must point the right way.

`DELETE friends/{uuid}/` and declining a request both leave the `Friendship` row
in place - `Removed` and `Declined` respectively - and `FriendshipStatus.can_request`
allows a new request from either. `Friendship.request` reuses that row rather
than creating a second one, which is correct (there should be one row per pair)
and was doing it without re-orienting the ends.

The consequence: B removes A, then B asks A to be friends again, and the revived
row still says A asked B. A's accept looks for an incoming request from B, finds
none, and answers "Friend request not found". Both people can see the request;
neither can act on it, permanently.

**Why the existing tests missed it, which is the transferable part.** Every
friendship test starts from nothing and builds the state it needs. This defect
requires a *prior* relationship in a particular end state, so no amount of
testing the happy path from a clean slate reaches it. `docs/TEST_COVERAGE_GAPS.md`
records it as its own category for that reason: not an adversarial input nobody
tried, but a starting state nobody started from.

Found by `tests/integration/specs/api/social.spec.ts`, where it showed up only
as the first test in the file - the one that inherited the previous run's data.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.services.social.friendship import accept_friend_request, remove_friend, request_or_accept_friendship


class RevivedRequestDirectionTests(TestCase):
    """A revived row belongs to whoever is asking now."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = baker.make(User).profile
        self.bob = baker.make(User).profile

    def _row(self) -> Friendship:
        """The single row describing this pair, whichever way round it points."""
        friendship = Friendship.objects.all().between(self.alice, self.bob)
        assert friendship is not None, "no friendship row exists between the pair"
        return friendship

    def test_re_requesting_after_removal_can_be_accepted(self) -> None:
        """The reported case, end to end through the services the API calls."""
        # Alice asks Bob, Bob accepts, Bob removes.
        request_or_accept_friendship(self.alice, self.bob)
        accept_friend_request(self.bob, self.alice)
        remove_friend(self.bob, self.alice)

        # Now Bob asks Alice. The surviving row is Alice->Bob.
        request_or_accept_friendship(self.bob, self.alice)

        row = self._row()
        self.assertEqual(row.status, FriendshipStatus.REQUESTED)
        self.assertEqual(
            row.from_profile_id,
            self.bob.pk,
            "the revived request is recorded as though the other person sent it, so its recipient cannot accept it",
        )

        # And the half that actually matters to a user: it can be accepted.
        accept_friend_request(self.alice, self.bob)
        self.assertEqual(self._row().status, FriendshipStatus.ACCEPTED)

    def test_re_requesting_after_a_decline_can_be_accepted(self) -> None:
        """`Declined` is the other status `can_request` admits."""
        from urbanlens.dashboard.services.social.friendship import reject_friend_request

        request_or_accept_friendship(self.alice, self.bob)
        reject_friend_request(self.bob, self.alice)

        request_or_accept_friendship(self.bob, self.alice)

        self.assertEqual(self._row().from_profile_id, self.bob.pk)
        accept_friend_request(self.alice, self.bob)
        self.assertEqual(self._row().status, FriendshipStatus.ACCEPTED)

    def test_re_requesting_in_the_same_direction_still_works(self) -> None:
        """The case that already worked must keep working."""
        request_or_accept_friendship(self.alice, self.bob)
        accept_friend_request(self.bob, self.alice)
        remove_friend(self.bob, self.alice)

        request_or_accept_friendship(self.alice, self.bob)

        self.assertEqual(self._row().from_profile_id, self.alice.pk)
        accept_friend_request(self.bob, self.alice)
        self.assertEqual(self._row().status, FriendshipStatus.ACCEPTED)

    def test_reviving_does_not_create_a_second_row(self) -> None:
        """One pair, one row - the reason `request` reuses rather than creates."""
        request_or_accept_friendship(self.alice, self.bob)
        accept_friend_request(self.bob, self.alice)
        remove_friend(self.bob, self.alice)
        request_or_accept_friendship(self.bob, self.alice)

        pair = Friendship.objects.filter(from_profile__in=[self.alice, self.bob], to_profile__in=[self.alice, self.bob])
        self.assertEqual(pair.count(), 1, "reviving a removed friendship produced a second row for the same pair")

    def test_a_mute_follows_its_owner_when_the_row_is_reoriented(self) -> None:
        """`muted_by_*` are positional, so they have to travel with the ends.

        Which column belongs to a viewer depends on which end of the row they
        are. Swapping the ends without swapping these hands Alice's mute to Bob
        - silencing the wrong person, which is worse than not muting at all
        because neither of them can see it happened.
        """
        request_or_accept_friendship(self.alice, self.bob)
        accept_friend_request(self.bob, self.alice)

        # Alice mutes Bob. Alice is `from_profile` on this row.
        row = self._row()
        self.assertEqual(row.from_profile_id, self.alice.pk)
        row.muted_by_from_profile = True
        row.save(update_fields=["muted_by_from_profile"])

        remove_friend(self.bob, self.alice)
        request_or_accept_friendship(self.bob, self.alice)

        revived = self._row()
        self.assertEqual(revived.from_profile_id, self.bob.pk, "precondition: the row should have been re-oriented")
        self.assertFalse(revived.muted_by_from_profile, "Bob is now `from_profile` and inherited Alice's mute")
        self.assertTrue(revived.muted_by_to_profile, "Alice's mute did not travel with her to the other end of the row")


class StatusesThatCannotBeRevivedTests(TestCase):
    """The other end of the same rule.

    `docs/TEST_COVERAGE_GAPS.md` suggested generalising the re-orientation
    above to every status `between()` can return - `Declined`, `Ignored`,
    `Blocked`. Only the first of those is right: `can_request` admits
    `Declined` and `Removed` and nothing else, so for `Blocked` and `Ignored`
    the correct behaviour is a *refusal*, and re-orienting one of those rows
    would be the defect rather than the fix.

    These exist because the re-orientation sits directly below that guard.
    Anyone widening one has to walk past a test saying why the other is narrow.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = baker.make(User).profile
        self.bob = baker.make(User).profile

    def _existing(self, status: str) -> Friendship:
        return Friendship.objects.create(from_profile=self.alice, to_profile=self.bob, status=status)

    def test_a_blocked_row_is_not_revived_or_reoriented(self) -> None:
        self._existing(FriendshipStatus.BLOCKED)

        self.assertIsNone(Friendship.request(self.bob, self.alice), "a blocked pair accepted a new friend request")

        row = Friendship.objects.all().between(self.alice, self.bob)
        assert row is not None
        self.assertEqual(row.status, FriendshipStatus.BLOCKED)
        self.assertEqual(row.from_profile_id, self.alice.pk, "a refused request still moved the row's ends")

    def test_an_ignored_row_is_not_revived_or_reoriented(self) -> None:
        self._existing(FriendshipStatus.IGNORED)

        self.assertIsNone(Friendship.request(self.bob, self.alice), "an ignored request could be re-sent by the other side")

        row = Friendship.objects.all().between(self.alice, self.bob)
        assert row is not None
        self.assertEqual(row.status, FriendshipStatus.IGNORED)
        self.assertEqual(row.from_profile_id, self.alice.pk, "a refused request still moved the row's ends")
