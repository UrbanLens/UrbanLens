"""One `Friendship` row per pair, enforced by the database rather than assumed.

`unique_together = ("from_profile", "to_profile")` stops a duplicate in one
direction and permits `A->B` *and* `B->A` to both exist. Every reader assumed
they could not: the model docstring says "exactly one row per pair", `between()`
matches either direction, and the mute columns are per-side of *one* row - so a
reciprocal pair split one relationship's state across two rows, and `between()`
had to pick.

Two properties, and the second is the one a constraint alone would not give:

- The pair cannot be created any more. Either direction is refused once the
  other exists, whichever way round it is written.
- The direction still means what it meant. `from_profile` is "who asked", which
  `Pending`/`Requested` and `request_message` depend on - so this is a
  constraint on the *ordered* pair rather than a normalisation of the columns
  into id order, which would have inverted that for half the table.

The merge rule that migration 0054 applies to rows that already exist is tested
here too, against the function itself: it has to be safe for every combination
of statuses, because nothing recorded which of two conflicting ones was right.
"""

from __future__ import annotations

import importlib
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.friendship.queryset import QuerySet as FriendshipQuerySet
from urbanlens.dashboard.models.profile.model import Profile

_MERGE = importlib.import_module("urbanlens.dashboard.migrations.0054_merge_reciprocal_friendships")


class FriendshipPairConstraintTests(TestCase):
    """The database refuses a second row for a pair, in either direction."""

    def setUp(self) -> None:
        super().setUp()
        self.a = Profile.objects.get(user=baker.make(User))
        self.b = Profile.objects.get(user=baker.make(User))

    def _make(self, sender: Profile, receiver: Profile, status: str = FriendshipStatus.ACCEPTED) -> Friendship:
        return Friendship.objects.create(
            from_profile=sender,
            to_profile=receiver,
            status=status,
            relationship_type=FriendshipType.FRIEND,
        )

    def test_the_same_direction_is_still_refused(self) -> None:
        self._make(self.a, self.b)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._make(self.a, self.b)

    def test_the_reverse_direction_is_refused_too(self) -> None:
        """The gap this closes: `unique_together` never saw this as a duplicate."""
        self._make(self.a, self.b)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._make(self.b, self.a)

    def test_a_different_pair_is_unaffected(self) -> None:
        third = Profile.objects.get(user=baker.make(User))
        self._make(self.a, self.b)
        self._make(self.a, third)
        self.assertEqual(Friendship.objects.filter(from_profile=self.a).count(), 2)

    def test_the_direction_is_preserved(self) -> None:
        """`from_profile` is "who asked", not "the lower id"."""
        lower, higher = sorted((self.a, self.b), key=lambda profile: profile.pk)
        row = self._make(higher, lower, status=FriendshipStatus.PENDING)
        row.refresh_from_db()
        self.assertEqual(row.from_profile_id, higher.pk, "normalising to id order would rewrite who sent the request")

    def test_a_request_that_loses_the_race_returns_the_row_that_won(self) -> None:
        """Two opposite requests at once: the constraint refuses the second.

        Before it, this produced two rows for one relationship with the mute
        columns split across them. `request()` now returns the winner rather
        than raising - the two people wanted the same thing.
        """
        winner = self._make(self.b, self.a, status=FriendshipStatus.REQUESTED)
        # Standing in for the interleaving: `between()` finds nothing, then the
        # insert collides with the row the other request already committed.
        with mock.patch.object(FriendshipQuerySet, "between", side_effect=[None, winner]):
            result = Friendship.request(self.a, self.b)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, winner.pk)
        self.assertEqual(Friendship.objects.count(), 1)

    def test_between_finds_it_from_either_side(self) -> None:
        row = self._make(self.a, self.b)
        self.assertEqual(Friendship.objects.between(self.a, self.b).pk, row.pk)
        self.assertEqual(Friendship.objects.between(self.b, self.a).pk, row.pk)


class ReciprocalMergeRuleTests(TestCase):
    """Migration 0054's rule for rows that already exist."""

    def test_the_more_restrictive_status_wins(self) -> None:
        for kept, other in (
            (FriendshipStatus.BLOCKED, FriendshipStatus.ACCEPTED),
            (FriendshipStatus.REMOVED, FriendshipStatus.ACCEPTED),
            (FriendshipStatus.DECLINED, FriendshipStatus.PENDING),
            (FriendshipStatus.IGNORED, FriendshipStatus.REQUESTED),
            (FriendshipStatus.ACCEPTED, FriendshipStatus.PENDING),
            (FriendshipStatus.ACCEPTED, FriendshipStatus.REQUESTED),
        ):
            with self.subTest(kept=kept, other=other):
                self.assertLess(_MERGE._rank(kept), _MERGE._rank(other), f"{kept} should outrank {other}")

    def test_an_explicit_no_is_never_undone_by_a_merge(self) -> None:
        """The property behind the order, stated once rather than per pair."""
        refusals = (
            FriendshipStatus.BLOCKED,
            FriendshipStatus.REMOVED,
            FriendshipStatus.DECLINED,
            FriendshipStatus.IGNORED,
        )
        permissive = (FriendshipStatus.ACCEPTED, FriendshipStatus.REQUESTED, FriendshipStatus.PENDING)
        for refusal in refusals:
            for allowed in permissive:
                with self.subTest(refusal=refusal, allowed=allowed):
                    self.assertLess(_MERGE._rank(refusal), _MERGE._rank(allowed))

    def test_blocked_outranks_every_other_status(self) -> None:
        for status in FriendshipStatus.values:
            if status != FriendshipStatus.BLOCKED:
                with self.subTest(status=status):
                    self.assertLess(_MERGE._rank(FriendshipStatus.BLOCKED), _MERGE._rank(status))

    def test_an_unrecognised_status_never_outranks_a_real_one(self) -> None:
        """A value from a future migration must not silently win."""
        for status in FriendshipStatus.values:
            with self.subTest(status=status):
                self.assertLess(_MERGE._rank(status), _MERGE._rank("SomethingNobodyHasWrittenYet"))

    def test_every_declared_status_is_ranked(self) -> None:
        """A new status added without a rank would sort last by accident."""
        unranked = [
            status for status in FriendshipStatus.values if _MERGE._rank(status) == len(_MERGE._STATUS_PRECEDENCE)
        ]
        self.assertEqual(unranked, [], "add these to _STATUS_PRECEDENCE deliberately")


class _Row:
    """A stand-in for one `Friendship` row, with only what the merge touches."""

    def __init__(
        self, pk: int, sender: int, receiver: int, status: str, muted_from: bool = False, muted_to: bool = False
    ) -> None:
        self.pk = pk
        self.from_profile_id = sender
        self.to_profile_id = receiver
        self.status = status
        self.muted_by_from_profile = muted_from
        self.muted_by_to_profile = muted_to
        self.deleted = False

    def save(self, update_fields=None) -> None:  # noqa: ARG002
        """Accepted and ignored; the test reads the attributes directly."""

    def delete(self) -> None:
        self.deleted = True


def _run_merge(rows: list[_Row]) -> None:
    """Run the migration's merge over `rows`, in the order given."""

    class _Manager:
        @staticmethod
        def order_by(*_args):
            class _QuerySet:
                @staticmethod
                def iterator():
                    return iter(rows)

            return _QuerySet()

    class _Apps:
        @staticmethod
        def get_model(*_args):
            return type("FriendshipStub", (), {"objects": _Manager})

    _MERGE.merge_reciprocal_rows(_Apps, None)


class ReciprocalMergeBehaviourTests(TestCase):
    """What the migration does to a pair that already exists.

    Exercised against the function rather than through a real migration run,
    because the constraint this ships with makes the pair uncreatable - which is
    the point of it, and also why this state can only be reached by a database
    that predates it.
    """

    def test_the_older_row_is_the_one_kept(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED)
        _run_merge([older, newer])
        self.assertFalse(older.deleted)
        self.assertTrue(newer.deleted)

    def test_the_restrictive_status_survives_from_the_discarded_row(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        newer = _Row(2, 20, 10, FriendshipStatus.BLOCKED)
        _run_merge([older, newer])
        self.assertEqual(older.status, FriendshipStatus.BLOCKED, "a block must not be undone by a merge")

    def test_a_permissive_status_does_not_overwrite_a_restrictive_one(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.REMOVED)
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED)
        _run_merge([older, newer])
        self.assertEqual(older.status, FriendshipStatus.REMOVED)

    def test_a_mute_on_the_reversed_row_lands_on_the_right_person(self) -> None:
        """The mute columns are per-side of a row, so a reversal swaps them."""
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        # Profile 20 is `from` on its own row, and `to` on the keeper's.
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED, muted_from=True)
        _run_merge([older, newer])
        self.assertFalse(older.muted_by_from_profile, "profile 10 never muted anyone")
        self.assertTrue(older.muted_by_to_profile, "profile 20's mute must survive on its own side")

    def test_mutes_from_both_rows_are_kept(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED, muted_from=True)
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED, muted_from=True)
        _run_merge([older, newer])
        self.assertTrue(older.muted_by_from_profile)
        self.assertTrue(older.muted_by_to_profile)

    def test_a_same_direction_duplicate_is_merged_without_swapping(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        newer = _Row(2, 10, 20, FriendshipStatus.ACCEPTED, muted_from=True)
        _run_merge([older, newer])
        self.assertTrue(older.muted_by_from_profile)
        self.assertFalse(older.muted_by_to_profile)

    def test_unrelated_pairs_are_left_alone(self) -> None:
        first = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        second = _Row(2, 10, 30, FriendshipStatus.ACCEPTED)
        _run_merge([first, second])
        self.assertFalse(first.deleted)
        self.assertFalse(second.deleted)
