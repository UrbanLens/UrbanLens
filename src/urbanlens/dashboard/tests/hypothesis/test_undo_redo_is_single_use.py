"""A redo entry must reapply once, however many times it is submitted.

Sibling of ``test_undo_restore_is_single_use.py``: ``redo_undo_action`` claims the row under the
same lock ``restore_undo_action`` uses and stamps ``undone_at`` back to ``None`` so a second submit
finds it already consumed. The restore-side property is covered there and proven with a real
double-submit; this file proves the identical guarantee holds for redo, which shares the locking
helper but was previously untested on its own.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.undo.service import UndoExpiredError, redo_undo_action, restore_undo_action, stash_for_undo


class UndoRedoIsSingleUseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.trip = baker.make(Trip, creator=self.profile, name="Quarry weekend")
        self.action = stash_for_undo("trip", [self.trip], self.profile)
        self.trip.delete()
        # Undo first, so the entry starts in the "undone, ready to redo" state
        # every test in this file exercises.
        restore_undo_action(self._fetch())

    def _fetch(self) -> UndoAction:
        """A fresh instance, as a second concurrent request would have."""
        return UndoAction.objects.get(pk=self.action.pk)

    def test_a_single_redo_deletes_the_trip_again(self) -> None:
        """Anchors the rest: the redo path works at all."""
        redo_undo_action(self._fetch())

        self.assertEqual(Trip.objects.filter(name="Quarry weekend").count(), 0)

    def test_a_double_submit_redoes_only_once(self) -> None:
        """The property: both requests hold a valid (undone) entry; only one may act."""
        first, second = self._fetch(), self._fetch()

        redo_undo_action(first)
        with self.assertRaises(UndoExpiredError):
            redo_undo_action(second)

        self.assertEqual(
            Trip.objects.filter(name="Quarry weekend").count(),
            0,
            "the redo ran twice",
        )

    def test_the_entry_leaves_the_redo_stack_after_a_successful_redo(self) -> None:
        redo_undo_action(self._fetch())

        action = UndoAction.objects.get(pk=self.action.pk)
        self.assertIsNone(action.undone_at)

    def test_the_second_attempt_leaves_the_redone_entry_in_place(self) -> None:
        """A refused second attempt must not resurrect the redo stack entry."""
        first, second = self._fetch(), self._fetch()
        redo_undo_action(first)

        with self.assertRaises(UndoExpiredError):
            redo_undo_action(second)

        action = UndoAction.objects.get(pk=self.action.pk)
        self.assertIsNone(action.undone_at)

    def test_the_redo_claims_the_row_under_a_lock(self) -> None:
        """The mechanism: without it, two real requests race rather than queue."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            redo_undo_action(self._fetch())

        locking = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"].upper()]
        self.assertTrue(locking, "the undo entry should be locked while it is claimed")
        self.assertTrue(
            any("undo" in sql.lower() for sql in locking),
            f"the lock should be on the undo row, got: {locking[:2]}",
        )
