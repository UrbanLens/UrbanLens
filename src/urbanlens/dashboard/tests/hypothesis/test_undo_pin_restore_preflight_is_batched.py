"""Undoing a bulk pin delete validates the batch one row at a time.

N21 H58. Before recreating anything, `PinUndoHandler.restore` walks the payload
checking that each pin's profile, location, wiki and labels still exist, and
that a root pin's location has not been re-pinned in the meantime. Every one of
those is a separate `.exists()`/`.count()` against a single id, so the pre-flight
costs five queries per pin - and `_resolved_parent_pk` adds a sixth for a parent
outside the batch. A bulk delete is capped at `_MAX_BULK_PINS` (500), so undoing
one could issue thousands of queries before the first row is recreated, all
inside the transaction holding the undo row's lock.

The creates themselves are inherently per-pin: restoring N rows writes N rows.
The *validation* is not, and that is what this covers - a batched pre-flight
asks the same questions with one query per relation rather than per pin.

The refusals it decides are the interesting part, not the count: they are what
stands between an undo and an `IntegrityError` on a constraint. Those semantics
are pinned by `test_undo_restore_conflicts` and `test_undo_pin_restore_conflict`
already; this file only adds the cost invariant, and a case for each refusal so
a batched check that stopped refusing would fail here too.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.handlers.pin import PinUndoHandler
from urbanlens.dashboard.services.undo.service import UndoExpiredError


class _PreflightCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile

    def _payload_for(self, count: int) -> list[dict[str, Any]]:
        pins = [baker.make(Pin, profile=self.profile, location=baker.make(Location)) for _ in range(count)]
        payload = PinUndoHandler.serialize(pins)
        Pin.objects.filter(pk__in=[pin.pk for pin in pins]).delete()
        return payload

    def _preflight_queries(self, payload: list[dict[str, Any]]) -> int:
        in_batch = {entry["old_pk"] for entry in payload}
        with CaptureQueriesContext(connection) as captured:
            PinUndoHandler.assert_restorable(payload, in_batch)
        return len(captured.captured_queries)


class ThePreflightDoesNotAskPerPinTests(_PreflightCase):
    """The same questions, asked once per relation rather than once per row."""

    def test_more_pins_do_not_cost_more_preflight_queries(self) -> None:
        small = self._preflight_queries(self._payload_for(2))
        large = self._preflight_queries(self._payload_for(20))

        self.assertLessEqual(
            large - small,
            1,
            f"eighteen more pins cost {large - small} more pre-flight queries",
        )


class TheRefusalsStillHappenTests(_PreflightCase):
    """A batched check that stopped refusing would be worse than the cost it saved."""

    def test_a_missing_location_is_still_refused(self) -> None:
        payload = self._payload_for(3)
        Location.objects.filter(pk=payload[0]["location_id"]).delete()

        with self.assertRaises(UndoExpiredError):
            PinUndoHandler.assert_restorable(payload, {entry["old_pk"] for entry in payload})

    def test_a_relocated_root_pin_is_still_refused(self) -> None:
        """The constraint this exists to keep off an IntegrityError."""
        payload = self._payload_for(3)
        baker.make(Pin, profile=self.profile, location_id=payload[0]["location_id"])

        with self.assertRaises(UndoExpiredError):
            PinUndoHandler.assert_restorable(payload, {entry["old_pk"] for entry in payload})

    def test_an_intact_batch_is_not_refused(self) -> None:
        """The anti-vacuity half: a check that always refused would pass both of the above."""
        payload = self._payload_for(3)

        PinUndoHandler.assert_restorable(payload, {entry["old_pk"] for entry in payload})

    def test_the_whole_restore_still_works_end_to_end(self) -> None:
        """The extraction must not change what `restore` does, only how it asks."""
        payload = self._payload_for(3)

        restored = PinUndoHandler.restore(payload)

        self.assertEqual(len(restored), 3)
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 3)
