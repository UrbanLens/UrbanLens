"""An undo entry must restore once, however many times it is submitted.

``restore_undo_action`` checks expiry, calls the handler's ``restore()``, then
deletes the entry - with nothing claiming the row in between. Both halves of a
double-submit fetch the entry while it still exists, so both pass the check and
both restore. The user asked for one undo and gets two copies of everything.

A double-click on Undo is the ordinary way in: the button issues a POST, and all
three entry points (the web undo view, the pin bulk-delete view, and the external
API) look the entry up and hand it straight to the service.

Whether a duplicate actually appears depends today on whether the *handler*
happens to hit a unique constraint on the way - ``PinUndoHandler`` is saved by
``db_pin_unique_location_per_profile`` for root pins, and the saved-filter, label,
wiki, pin-list, markup-map and safety-checkin handlers all re-check something
before recreating. ``TripUndoHandler.restore`` does an unconditional
``Trip.objects.create``, so it duplicates outright. That difference is exactly why
the guard belongs in the service rather than in each handler: a new handler
without a convenient unique constraint silently inherits the bug.
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

    def test_the_entry_is_gone_after_a_successful_restore(self) -> None:
        restore_undo_action(self._fetch())

        self.assertFalse(UndoAction.objects.filter(pk=self.action.pk).exists())

    def test_the_second_attempt_leaves_no_extra_entry_behind(self) -> None:
        """A refused second attempt must not resurrect or orphan the entry."""
        first, second = self._fetch(), self._fetch()
        restore_undo_action(first)

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(second)

        self.assertFalse(UndoAction.objects.filter(pk=self.action.pk).exists())

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
