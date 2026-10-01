"""A block records when it was placed, and one helper answers what it hides in a shared space (I7)."""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.blocks import SharedSpaceBlocks
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social.friendship import block_profile, mute_profile, unblock_profile


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _row(a: Profile, b: Profile) -> Friendship:
    row = Friendship.objects.all().between(a, b)
    assert row is not None
    return row


class BlockedAtTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = _profile()
        self.bob = _profile()

    def test_a_new_block_records_when_it_was_placed(self) -> None:
        before = timezone.now()
        block_profile(self.alice, self.bob)

        row = _row(self.alice, self.bob)
        self.assertIsNotNone(row.blocked_at)
        self.assertGreaterEqual(row.blocked_at, before)

    def test_blocking_a_friend_records_the_block_not_the_friendship(self) -> None:
        friendship = Friendship.request(self.alice, self.bob)
        assert friendship is not None
        friendship.accept()
        Friendship.objects.filter(pk=friendship.pk).update(created=timezone.now() - timedelta(days=30))

        before = timezone.now()
        block_profile(self.bob, self.alice)

        row = _row(self.alice, self.bob)
        self.assertEqual(row.status, FriendshipStatus.BLOCKED)
        self.assertGreaterEqual(row.blocked_at, before)

    def test_blocking_back_keeps_the_first_block_time(self) -> None:
        block_profile(self.alice, self.bob)
        first = _row(self.alice, self.bob).blocked_at

        block_profile(self.bob, self.alice)

        self.assertEqual(_row(self.alice, self.bob).blocked_at, first)

    def test_muting_a_blocked_row_leaves_the_block_time_alone(self) -> None:
        block_profile(self.alice, self.bob)
        first = _row(self.alice, self.bob).blocked_at

        mute_profile(self.alice, self.bob)

        self.assertEqual(_row(self.alice, self.bob).blocked_at, first)

    def test_lifting_a_block_clears_it_and_a_new_block_starts_again(self) -> None:
        block_profile(self.alice, self.bob)
        first = _row(self.alice, self.bob).blocked_at

        unblock_profile(self.alice, self.bob)
        self.assertIsNone(_row(self.alice, self.bob).blocked_at)

        block_profile(self.alice, self.bob)
        self.assertGreater(_row(self.alice, self.bob).blocked_at, first)

    def test_the_legacy_classmethod_also_records_it(self) -> None:
        Friendship.block(self.alice, self.bob)

        self.assertIsNotNone(_row(self.alice, self.bob).blocked_at)

    def test_a_blocked_row_without_a_time_is_refused_by_the_database(self) -> None:
        """A queryset ``update()`` skips ``save()``; the constraint is what still holds it."""
        friendship = Friendship.request(self.alice, self.bob)
        assert friendship is not None

        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.filter(pk=friendship.pk).update(status=FriendshipStatus.BLOCKED)

    def test_a_status_saved_from_a_stale_copy_writes_the_block_time_with_it(self) -> None:
        friendship = Friendship.request(self.alice, self.bob)
        assert friendship is not None
        friendship.accept()
        stale = Friendship.objects.get(pk=friendship.pk)
        block_profile(self.alice, self.bob)

        stale.remove()

        row = _row(self.alice, self.bob)
        self.assertEqual(row.blocked_at is not None, row.status == FriendshipStatus.BLOCKED)

    def test_a_lifted_block_keeping_its_time_is_refused_by_the_database(self) -> None:
        """Left in place, the stale time would become the cutoff of the next block ``save()`` places."""
        block_profile(self.alice, self.bob)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.filter(pk=_row(self.alice, self.bob).pk).update(status=FriendshipStatus.REMOVED)


class SharedSpaceBlocksTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = _profile()
        self.bob = _profile()
        self.carol = _profile()

    def test_nobody_is_hidden_without_a_block(self) -> None:
        blocks = SharedSpaceBlocks.for_viewer(self.alice)

        self.assertFalse(blocks.hides_profile(self.bob.pk))
        self.assertFalse(blocks.hides_content(self.bob.pk, timezone.now()))

    def test_either_direction_hides_the_pair_from_each_other(self) -> None:
        block_profile(self.alice, self.bob)

        self.assertTrue(SharedSpaceBlocks.for_viewer(self.alice).hides_profile(self.bob.pk))
        self.assertTrue(SharedSpaceBlocks.for_viewer(self.bob).hides_profile(self.alice.pk))
        self.assertFalse(SharedSpaceBlocks.for_viewer(self.carol).hides_profile(self.alice.pk))

    def test_content_from_before_the_block_stays_visible(self) -> None:
        block_profile(self.alice, self.bob)
        blocked_at = _row(self.alice, self.bob).blocked_at
        blocks = SharedSpaceBlocks.for_viewer(self.alice)

        self.assertFalse(blocks.hides_content(self.bob.pk, blocked_at - timedelta(seconds=1)))
        self.assertTrue(blocks.hides_content(self.bob.pk, blocked_at))
        self.assertTrue(blocks.hides_content(self.bob.pk, blocked_at + timedelta(hours=1)))

    def test_nobody_hides_their_own_content(self) -> None:
        block_profile(self.alice, self.bob)

        self.assertFalse(SharedSpaceBlocks.for_viewer(self.alice).hides_content(self.alice.pk, timezone.now()))

    def test_a_lifted_block_hides_nothing(self) -> None:
        block_profile(self.alice, self.bob)
        unblock_profile(self.alice, self.bob)

        self.assertFalse(SharedSpaceBlocks.for_viewer(self.alice).hides_profile(self.bob.pk))

    def test_one_query_resolves_a_whole_membership(self) -> None:
        block_profile(self.alice, self.bob)
        block_profile(self.carol, self.alice)
        dave = _profile()

        with CaptureQueriesContext(connection) as ctx:
            by_viewer = SharedSpaceBlocks.for_profiles([self.alice.pk, self.bob.pk, self.carol.pk, dave.pk])

        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual(by_viewer[self.alice.pk].hidden_profile_ids, {self.bob.pk, self.carol.pk})
        self.assertEqual(by_viewer[self.bob.pk].hidden_profile_ids, {self.alice.pk})
        self.assertEqual(by_viewer[dave.pk].hidden_profile_ids, frozenset())
