"""Undoing a pin delete must cope with the location having been re-pinned since.

``PinUndoHandler.restore`` pre-checks every foreign key the batch referenced - profile,
location, wiki, labels - and raises ``UndoExpiredError`` rather than letting the
recreate fail with an uncaught IntegrityError. It does not check
``db_pin_unique_location_per_profile``: one root pin per location per profile.

A user who deletes a pin, drops a new one at the same place, and then hits undo trips
exactly that constraint - which is an ordinary sequence, not a contrived one.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action, stash_for_undo


class UndoPinRestoreConflictTests(TestCase):
    """A restore that cannot fit is refused cleanly, not crashed through."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.location = Location.objects.create(latitude=47.3, longitude=-68.9)

    def _delete_with_undo(self, pin: Pin):
        undo_action = stash_for_undo("pin", [pin], self.profile)
        pin.delete()
        return undo_action

    def test_an_ordinary_undo_still_restores_the_pin(self):
        pin = Pin.objects.create(profile=self.profile, location=self.location, name="Old mill")
        undo_action = self._delete_with_undo(pin)

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Old mill")
        self.assertNotEqual(restored[0].pk, pin.pk)

    def test_undo_is_refused_when_the_location_has_been_re_pinned(self):
        # Delete, drop a new pin on the same place, then undo.
        pin = Pin.objects.create(profile=self.profile, location=self.location, name="Old mill")
        undo_action = self._delete_with_undo(pin)
        Pin.objects.create(profile=self.profile, location=self.location, name="Replacement")

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(undo_action)

    def test_the_replacement_pin_survives_the_refused_undo(self):
        pin = Pin.objects.create(profile=self.profile, location=self.location, name="Old mill")
        undo_action = self._delete_with_undo(pin)
        replacement = Pin.objects.create(profile=self.profile, location=self.location, name="Replacement")

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(undo_action)

        replacement.refresh_from_db()
        self.assertEqual(replacement.name, "Replacement")

    def test_another_profiles_pin_at_the_same_location_does_not_block_the_undo(self):
        # The constraint is per profile, so someone else pinning the place is irrelevant.
        pin = Pin.objects.create(profile=self.profile, location=self.location, name="Old mill")
        undo_action = self._delete_with_undo(pin)
        other: Profile = baker.make("auth.User").profile
        Pin.objects.create(profile=other, location=self.location, name="Someone else's")

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)

    def test_a_child_pin_at_a_re_pinned_location_still_restores(self):
        # The constraint only covers root pins (parent_pin IS NULL), so a detail pin
        # must not be refused just because the place has a root pin again.
        parent = Pin.objects.create(profile=self.profile, location=self.location, name="Site")
        child_location = Location.objects.create(latitude=47.31, longitude=-68.91)
        child = Pin.objects.create(profile=self.profile, location=child_location, name="Outbuilding", parent_pin=parent)
        undo_action = self._delete_with_undo(child)
        Pin.objects.create(profile=self.profile, location=child_location, name="New root here")

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Outbuilding")
