"""An undo entry must restore once, however many times it is submitted.

``restore_undo_action`` claims the row under a lock and stamps ``undone_at``
so a second submit finds it already consumed. Both halves of a double-submit
fetch the entry while it still exists, so without the claim both would restore
and the user would get two copies of everything.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action, stash_for_undo


class UndoRestoreIsSingleUseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.trip = baker.make(Trip, creator=self.profile, name="Quarry weekend")
        self.action = stash_for_undo("trip", [self.trip], self.profile)
        self.trip.delete()

    def _fetch(self) -> UndoAction:
        """A fresh instance, as a second concurrent request would have."""
        return UndoAction.objects.get(pk=self.action.pk)

    def test_a_single_undo_restores_the_trip(self) -> None:
        """Anchors the rest: the restore path works at all."""
        restore_undo_action(self._fetch())

        self.assertEqual(Trip.objects.filter(name="Quarry weekend").count(), 1)

    def test_a_double_submit_restores_only_once(self) -> None:
        """The property: both requests hold a valid entry; only one may act."""
        first, second = self._fetch(), self._fetch()

        restore_undo_action(first)
        with self.assertRaises(UndoExpiredError):
            restore_undo_action(second)

        self.assertEqual(
            Trip.objects.filter(name="Quarry weekend").count(),
            1,
            "the undo ran twice and restored two copies",
        )

    def test_the_entry_stays_on_the_redo_stack_after_a_successful_restore(self) -> None:
        restore_undo_action(self._fetch())

        action = UndoAction.objects.get(pk=self.action.pk)
        self.assertIsNotNone(action.undone_at)

    def test_the_second_attempt_leaves_the_undone_entry_in_place(self) -> None:
        """A refused second attempt must not resurrect or orphan the entry."""
        first, second = self._fetch(), self._fetch()
        restore_undo_action(first)

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(second)

        action = UndoAction.objects.get(pk=self.action.pk)
        self.assertIsNotNone(action.undone_at)

    def test_the_restore_claims_the_row_under_a_lock(self) -> None:
        """The mechanism: without it, two real requests race rather than queue."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            restore_undo_action(self._fetch())

        locking = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"].upper()]
        self.assertTrue(locking, "the undo entry should be locked while it is claimed")
        self.assertTrue(
            any("undo" in sql.lower() for sql in locking),
            f"the lock should be on the undo row, got: {locking[:2]}",
        )
