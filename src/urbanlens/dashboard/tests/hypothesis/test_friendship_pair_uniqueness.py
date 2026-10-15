"""One `Friendship` row per pair, enforced by the database rather than assumed."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.friendship.queryset import QuerySet as FriendshipQuerySet
from urbanlens.dashboard.models.profile.model import Profile


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
        """
        Two opposite requests at once: the constraint refuses the second.

        Before it, this produced two rows for one relationship with the mute columns split across them."""
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
