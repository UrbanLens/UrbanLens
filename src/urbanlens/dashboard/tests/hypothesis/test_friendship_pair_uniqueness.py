"""One `Friendship` row per pair, enforced by the database rather than assumed."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, connection, migrations, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import migrations as migrations_package
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.friendship.queryset import QuerySet as FriendshipQuerySet
from urbanlens.dashboard.models.profile.model import Profile

_MERGE = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")


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
        self,
        pk: int,
        sender: int,
        receiver: int,
        status: str,
        muted_from: bool = False,
        muted_to: bool = False,
        message: str | None = None,
    ) -> None:
        self.pk = pk
        self.from_profile_id = sender
        self.to_profile_id = receiver
        self.status = status
        self.muted_by_from_profile = muted_from
        self.muted_by_to_profile = muted_to
        self.request_message = message
        self.deleted = False

    def save(self, update_fields=None) -> None:  # noqa: ARG002
        """Accepted and ignored; the test reads the attributes directly."""

    def delete(self) -> None:
        self.deleted = True


def _run_merge(rows: list[_Row]) -> None:
    """Run the migration's merge over `rows`, in the order given.

    `_duplicated_pairs` is stubbed rather than reimplemented: it is one
    `GROUP BY ... HAVING COUNT(*) > 1`, and the behaviour under test is the
    merge, not the query that finds candidates.
    """

    class _QuerySet:
        @staticmethod
        def order_by(*_args):
            return _QuerySet

        @staticmethod
        def iterator():
            return iter(rows)

    class _Manager:
        @staticmethod
        def filter(*_args, **_kwargs):
            return _QuerySet

    pairs: list[tuple[int, int]] = []
    counts: dict[tuple[int, int], int] = {}
    for row in rows:
        key = (min(row.from_profile_id, row.to_profile_id), max(row.from_profile_id, row.to_profile_id))
        counts[key] = counts.get(key, 0) + 1
    pairs = [key for key, count in counts.items() if count > 1]

    class _Apps:
        @staticmethod
        def get_model(*_args):
            return type("FriendshipStub", (), {"objects": _Manager})

    original = _MERGE._duplicated_pairs
    _MERGE._duplicated_pairs = lambda _model: pairs
    try:
        _MERGE._0054_merge_reciprocal_rows(_Apps, None)
    finally:
        _MERGE._duplicated_pairs = original


class ReciprocalMergeBehaviourTests(TestCase):
    """What the migration does to a pair that already exists.

    Exercised against the function rather than through a real migration run, because the constraint this ships
    with makes the pair uncreatable - which is the point of it, and also why this state can only be reached by a
    database that predates it."""

    def test_the_keeper_is_the_row_between_would_have_answered_with(self) -> None:
        """Lowest pk, which is what `between()` has been returning."""
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED)
        _run_merge([older, newer])
        self.assertFalse(older.deleted)
        self.assertTrue(newer.deleted)

        # `Friendship.objects.between` resolves the same way, so the migration
        # keeps the identity the application has been using rather than one it
        # has never answered with.
        self.assertIn("pk", inspect.getsource(FriendshipQuerySet.between))

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

    def test_an_adopted_block_brings_its_direction_with_it(self) -> None:
        """The one way this migration could corrupt rather than merge.

        `Friendship` has no "blocked_by" column - `from_profile` *is* the blocker - so taking the reversed row's
        BLOCKED status without its ends records the blocked party as the blocker."""
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED)
        # Profile 20 blocked profile 10.
        newer = _Row(2, 20, 10, FriendshipStatus.BLOCKED)
        _run_merge([older, newer])
        self.assertEqual(older.status, FriendshipStatus.BLOCKED)
        self.assertEqual(older.from_profile_id, 20, "the blocker must stay the blocker")
        self.assertEqual(older.to_profile_id, 10)

    def test_an_adopted_request_brings_its_asker_and_message(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.PENDING, message="from ten")
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED, message="from twenty")
        _run_merge([older, newer])
        self.assertEqual(older.status, FriendshipStatus.ACCEPTED)
        self.assertEqual(older.from_profile_id, 20)
        self.assertEqual(older.request_message, "from twenty")

    def test_a_swap_carries_the_keeper_own_mutes_with_it(self) -> None:
        """Flipping the ends must not hand one person's mute to the other."""
        # Profile 10 muted; then profile 20 blocked.
        older = _Row(1, 10, 20, FriendshipStatus.ACCEPTED, muted_from=True)
        newer = _Row(2, 20, 10, FriendshipStatus.BLOCKED)
        _run_merge([older, newer])
        self.assertEqual(older.from_profile_id, 20)
        self.assertFalse(older.muted_by_from_profile, "profile 20 never muted anyone")
        self.assertTrue(older.muted_by_to_profile, "profile 10's mute follows it to the `to` side")

    def test_no_swap_when_the_keeper_status_already_wins(self) -> None:
        older = _Row(1, 10, 20, FriendshipStatus.BLOCKED)
        newer = _Row(2, 20, 10, FriendshipStatus.ACCEPTED)
        _run_merge([older, newer])
        self.assertEqual(older.from_profile_id, 10, "the keeper's own block direction must be left alone")


class _RealApps:
    """`apps` as the merge receives it, answering with the live `Friendship`.

    The historical model differs from this one only by the constraint the
    fixture below drops; the merge touches no field that has changed since.
    """

    @staticmethod
    def get_model(*_args: str) -> type[Friendship]:
        """The one model the merge asks for."""
        return Friendship


class ReciprocalMergeAgainstTheDatabaseTests(TestCase):
    """The merge run over real rows, against the constraints production carries.

    `ReciprocalMergeBehaviourTests` above drives the merge through `_Row`, whose `save()` is a no-op - so none
    of those tests can see a database constraint, the swap cases included."""

    def setUp(self) -> None:
        super().setUp()
        self.a = Profile.objects.get(user=baker.make(User))
        self.b = Profile.objects.get(user=baker.make(User))
        constraint = next(item for item in Friendship._meta.constraints if item.name == "friendship_one_row_per_pair")
        with connection.schema_editor(atomic=False) as editor:
            editor.remove_constraint(Friendship, constraint)

    def _pair(
        self,
        sender: Profile,
        receiver: Profile,
        keeper_status: str,
        loser_status: str,
    ) -> tuple[Friendship, Friendship]:
        """A reciprocal pair. The keeper is written first, so it holds the lower pk."""
        keeper = Friendship.objects.create(
            from_profile=sender,
            to_profile=receiver,
            status=keeper_status,
            relationship_type=FriendshipType.FRIEND,
        )
        loser = Friendship.objects.create(
            from_profile=receiver,
            to_profile=sender,
            status=loser_status,
            relationship_type=FriendshipType.FRIEND,
        )
        return keeper, loser

    def test_the_pair_can_be_created_once_the_constraint_is_dropped(self) -> None:
        """Anti-vacuity: otherwise every merge below could pass over an empty table."""
        keeper, loser = self._pair(self.a, self.b, FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED)

        self.assertEqual(Friendship.objects.count(), 2)
        self.assertLess(keeper.pk, loser.pk, "the keeper is the lowest-pk row, which is what the merge assumes")

    def test_a_pair_whose_losing_row_holds_the_restrictive_status_merges(self) -> None:
        """The shape the migration exists for, and the one that aborted a release.

        `a -> b Accepted` and `b -> a Blocked`: the block wins, so the keeper's
        ends swap to `(b, a)` - which is the loser's own key.
        """
        keeper, _loser = self._pair(self.a, self.b, FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED)

        _MERGE._0054_merge_reciprocal_rows(_RealApps, None)

        self.assertEqual(Friendship.objects.count(), 1)
        survivor = Friendship.objects.get()
        self.assertEqual(survivor.pk, keeper.pk)
        self.assertEqual(survivor.status, FriendshipStatus.BLOCKED)
        self.assertEqual(survivor.from_profile_id, self.b.pk, "the blocker must stay the blocker")
        self.assertEqual(survivor.to_profile_id, self.a.pk)

    def test_a_pair_whose_keeper_already_wins_merges_too(self) -> None:
        """The non-swapping case - which passed all along, and is the control for it."""
        keeper, _loser = self._pair(self.a, self.b, FriendshipStatus.BLOCKED, FriendshipStatus.ACCEPTED)

        _MERGE._0054_merge_reciprocal_rows(_RealApps, None)

        survivor = Friendship.objects.get()
        self.assertEqual(survivor.pk, keeper.pk)
        self.assertEqual(survivor.from_profile_id, self.a.pk, "the keeper's own direction must be left alone")
        self.assertEqual(survivor.status, FriendshipStatus.BLOCKED)

    def test_the_discarded_row_is_still_named_in_the_log(self) -> None:
        """On a real database the log line is the only trace the row existed.

        `Model.delete()` sets the instance's pk to `None`, so deleting before
        logging reports "row None" - losing exactly what these warnings are for.
        """
        _keeper, loser = self._pair(self.a, self.b, FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED)

        with self.assertLogs(_MERGE.logger.name, level="WARNING") as captured:
            _MERGE._0054_merge_reciprocal_rows(_RealApps, None)

        deletions = [line for line in captured.output if "Deleting reciprocal friendship row" in line]
        self.assertEqual(len(deletions), 1, "one row was discarded, so one line records it")
        self.assertIn(f"row {loser.pk} ", deletions[0], "the discarded row's id must survive its deletion")

    def test_the_merge_and_the_constraint_cannot_share_a_transaction(self) -> None:
        """Why they are two migrations - demonstrated, not asserted.

        The merge's UPDATE and DELETE queue deferred FK trigger events, and Postgres refuses to build an index
        over a table holding them."""
        self._pair(self.a, self.b, FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED)
        constraint = next(item for item in Friendship._meta.constraints if item.name == "friendship_one_row_per_pair")
        _MERGE._0054_merge_reciprocal_rows(_RealApps, None)

        with (
            self.assertRaises(OperationalError) as raised,
            transaction.atomic(),
            connection.schema_editor(atomic=False) as editor,
        ):
            editor.add_constraint(Friendship, constraint)

        self.assertIn("pending trigger events", str(raised.exception))

    def test_every_pair_is_merged_not_just_the_first(self) -> None:
        """An abort on pair one leaves the rest of the table untouched.

        The reported failure had three pairs and reached one, which is what makes
        a partial run worth asserting against rather than a single merge.
        """
        third = Profile.objects.get(user=baker.make(User))
        fourth = Profile.objects.get(user=baker.make(User))
        fifth = Profile.objects.get(user=baker.make(User))
        sixth = Profile.objects.get(user=baker.make(User))
        self._pair(self.a, self.b, FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED)
        self._pair(third, fourth, FriendshipStatus.REQUESTED, FriendshipStatus.ACCEPTED)
        self._pair(fifth, sixth, FriendshipStatus.PENDING, FriendshipStatus.IGNORED)

        _MERGE._0054_merge_reciprocal_rows(_RealApps, None)

        self.assertEqual(Friendship.objects.count(), 3, "one row per pair, and every pair reached")
        self.assertEqual(
            sorted(Friendship.objects.values_list("status", flat=True)),
            sorted([FriendshipStatus.BLOCKED, FriendshipStatus.ACCEPTED, FriendshipStatus.IGNORED]),
            "each pair keeps the more restrictive of its two statuses",
        )


class IndexWorkLivesInItsOwnMigrationTests(SimpleTestCase):
    """The merge and the constraint it clears the way for must stay in two migrations.

    A release squash collapses a branch's migrations into one file, and one file is one transaction - which is
    the whole hazard: see
    `ReciprocalMergeAgainstTheDatabaseTests.test_the_merge_and_the_constraint_cannot_share_a_transaction` for
    the failure itself."""

    def _module_of(self, predicate) -> str:
        """The one migration module whose operations satisfy `predicate`."""
        directory = Path(migrations_package.__file__).resolve().parent
        matches = [
            path.stem
            for path in sorted(directory.glob("[0-9]*.py"))
            if any(
                predicate(op)
                for op in importlib.import_module(f"urbanlens.dashboard.migrations.{path.stem}").Migration.operations
            )
        ]
        self.assertEqual(len(matches), 1, f"expected exactly one migration to match, got {matches}")
        return matches[0]

    def test_the_constraint_is_not_in_the_migration_that_merges(self) -> None:
        merge = self._module_of(
            lambda op: isinstance(op, migrations.RunPython) and op.code is _MERGE._0054_merge_reciprocal_rows
        )
        constraint = self._module_of(
            lambda op: isinstance(op, migrations.AddConstraint) and op.constraint.name == "friendship_one_row_per_pair"
        )

        self.assertNotEqual(
            merge, constraint, "one migration is one transaction, and the constraint cannot share it with the merge"
        )
        self.assertLess(
            merge, constraint, "and the merge has to run first, or the constraint rejects the rows it exists to merge"
        )

    #: Release squashes that predate the split, listed rather than fixed. Every
    #: one is already applied on every database that has them, and a database
    #: applying them for the first time is empty - so their backfills touch no
    #: rows and queue no trigger events. Rewriting an applied migration to satisfy
    #: a guard is the more dangerous move. The guard is here for the next squash.
    SETTLED = frozenset({"0003_v0_4_0_data", "0005_v0_4_0_pin_location_dedupe", "0010_v0_6_0", "0030_v0_7_0"})

    def test_no_new_release_migration_creates_an_index_beside_a_data_migration(self) -> None:
        """The general form of the rule, for the files a squash produces.

        Scoped to release squashes rather than the whole directory: a hand-written migration pairing a backfill
        with an index on a table it just created is safe and commonplace, and flagging those would make this all
        noise."""
        directory = Path(migrations_package.__file__).resolve().parent
        offenders = []
        for path in sorted(directory.glob("[0-9]*_v[0-9]*.py")):
            if path.stem.endswith("_indexes") or path.stem in self.SETTLED:
                continue
            operations = importlib.import_module(f"urbanlens.dashboard.migrations.{path.stem}").Migration.operations
            has_data = any(isinstance(op, migrations.RunPython | migrations.RunSQL) for op in operations)
            indexing = [op for op in operations if isinstance(op, migrations.AddIndex | migrations.AddConstraint)]
            if has_data and indexing:
                offenders.append(f"{path.stem}: {len(indexing)} index/constraint op(s) alongside a data migration")

        self.assertEqual(
            offenders, [], "move these into the release's `_indexes` companion - a second transaction is the point"
        )

    def test_the_settled_list_still_names_files_that_exist(self) -> None:
        """An exemption for a migration that is gone hides a real one behind it."""
        directory = Path(migrations_package.__file__).resolve().parent
        present = {path.stem for path in directory.glob("[0-9]*.py")}

        self.assertEqual(self.SETTLED - present, set(), "these were squashed away; drop them from SETTLED")
