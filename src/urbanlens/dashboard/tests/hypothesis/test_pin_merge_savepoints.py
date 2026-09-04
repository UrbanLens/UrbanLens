"""`merge_pins`' collision recoveries have to run inside savepoints.

Every reassignment in `services.pins.pin_merge` runs inside one
`transaction.atomic()` block, and eight of them were written as

    try:
        row.save(update_fields=["pin", "updated"])
    except IntegrityError:
        row.delete()          # drop the duplicate, carry on

Postgres aborts the *whole* transaction on a failed statement, so the recovery
query itself raised `TransactionManagementError: You can't execute queries
until the end of the 'atomic' block`. Every one of those graceful "drop the
duplicate" paths was therefore dead code, and any merge that hit a uniqueness
collision failed outright rather than deduping.

Reproduced before the fix by merging a pin into its own descendant while
another top-level pin occupied the location a child had to be detached to:
the merge raised `TransactionManagementError`, not the intended recovery.

See PROBLEMS.md, "merge_pins' IntegrityError recoveries could not run".
"""

from __future__ import annotations

import contextlib

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.transaction import TransactionManagementError
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.pin_merge import (
    PinMergeCollisionError,
    _save_within_savepoint,
    merge_pins,
)


class _PinFixtures(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self._locations = 0

    def location(self):
        self._locations += 1
        return baker.make(
            "dashboard.Location",
            latitude=35.0 + self._locations * 0.01,
            longitude=-80.0 - self._locations * 0.01,
        )


class SavepointKeepsTheTransactionUsableTests(_PinFixtures):
    """The property the eight recoveries depend on, asserted directly."""

    def test_a_collision_returns_false_and_leaves_the_transaction_usable(self) -> None:
        shared = self.location()
        baker.make(Pin, profile=self.profile, location=shared, parent_pin=None)
        mover = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)

        with transaction.atomic():
            mover.location = shared
            saved = _save_within_savepoint(mover, ["location", "updated"])
            self.assertFalse(saved, "the colliding save reported success")
            # The whole point: the caller's recovery runs *after* this.
            self.assertTrue(Pin.objects.filter(pk=mover.pk).exists())

    def test_a_clean_save_still_reports_success_and_persists(self) -> None:
        """Anti-vacuity: the helper must not simply always return False."""
        mover = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        fresh = self.location()

        with transaction.atomic():
            mover.location = fresh
            self.assertTrue(_save_within_savepoint(mover, ["location", "updated"]))

        mover.refresh_from_db()
        self.assertEqual(mover.location_id, fresh.pk)

    def test_the_savepoint_is_load_bearing(self) -> None:
        """Control: the shape the helper replaces really does poison the transaction.

        Without this, the tests above would pass just as well against a plain
        `try/except` and would prove nothing about why the helper exists.
        """
        shared = self.location()
        baker.make(Pin, profile=self.profile, location=shared, parent_pin=None)
        mover = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)

        with transaction.atomic(), self.assertRaises(TransactionManagementError):
            mover.location = shared
            with contextlib.suppress(IntegrityError):
                mover.save(update_fields=["location", "updated"])
            Pin.objects.filter(pk=mover.pk).exists()


class MergeRefusesRatherThanDestroyingTheSurvivorTests(_PinFixtures):
    """A child that cannot be detached must stop the merge, not be carried into it.

    `_reparent_children` detaches a child to top level when re-parenting it
    under the survivor would close a loop - which happens exactly when the
    survivor sits *beneath* that child. Leaving it parented to the loser is not
    a survivable fallback: `Pin.parent_pin` CASCADEs, so `loser.delete()` would
    take the child and the survivor with it. Before the savepoint fix this was
    masked, because the poisoned transaction raised first.
    """

    def setUp(self) -> None:
        super().setUp()
        self.shared = self.location()
        self.blocker = baker.make(Pin, profile=self.profile, location=self.shared, parent_pin=None)
        self.loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        # Legal while it is a child - the uniqueness constraint on
        # (location, profile) is conditional on parent_pin IS NULL.
        self.child = baker.make(Pin, profile=self.profile, location=self.shared, parent_pin=self.loser)
        self.survivor = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=self.child)

    def test_the_merge_is_refused_with_a_reason(self) -> None:
        with self.assertRaises(PinMergeCollisionError) as caught:
            merge_pins(self.survivor, self.loser, self.profile)
        self.assertIn("already occupies its location", caught.exception.safe_message)

    def test_nothing_is_deleted(self) -> None:
        with self.assertRaises(PinMergeCollisionError):
            merge_pins(self.survivor, self.loser, self.profile)

        for pin in (self.blocker, self.loser, self.child, self.survivor):
            self.assertTrue(Pin.objects.filter(pk=pin.pk).exists(), f"pin {pin.pk} was destroyed by a refused merge")

    def test_the_survivor_keeps_its_parent(self) -> None:
        with self.assertRaises(PinMergeCollisionError):
            merge_pins(self.survivor, self.loser, self.profile)

        self.survivor.refresh_from_db()
        self.assertEqual(self.survivor.parent_pin_id, self.child.pk)


class OrdinaryMergeStillWorksTests(_PinFixtures):
    """Anti-vacuity for the class above: the normal path must be untouched."""

    def test_a_plain_merge_moves_the_children_and_deletes_the_loser(self) -> None:
        survivor = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        loser = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=None)
        child = baker.make(Pin, profile=self.profile, location=self.location(), parent_pin=loser)

        merge_pins(survivor, loser, self.profile)

        child.refresh_from_db()
        self.assertEqual(child.parent_pin_id, survivor.pk)
        self.assertFalse(Pin.objects.filter(pk=loser.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=survivor.pk).exists())
