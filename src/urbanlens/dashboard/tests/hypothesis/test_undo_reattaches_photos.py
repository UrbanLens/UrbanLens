"""Undoing a pin delete must put its photos back on it.

``Image.pin`` is ``SET_NULL``, not ``CASCADE`` - deleting a pin deliberately
keeps the user's photos and merely detaches them, unlike comments, albums,
overlays and links, which the delete destroys. But the undo handler serialized
only the pin's own fields and label ids, so an undo restored the pin *empty*
while its photos sat unattached in the library, with nothing left recording
which pin they had been on.

The ids are captured at stash time because that is the only moment the link
still exists. On restore, only photos that are *still* detached are re-linked: a
photo the user has since attached to another pin belongs where they put it.

What this does not restore is everything that CASCADEs - comments, albums,
overlays, links, notes, visits. Bringing those back means serialising whole
object graphs, which is a much larger change; the limitation is recorded in
docs/PROBLEMS.md rather than half-implemented here.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.pin_edit import delete_pin
from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo


class UndoReattachesPhotosTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Doomed")
        self.image = baker.make(Image, profile=self.profile, pin=self.pin)

    def _delete_and_undo(self) -> Pin:
        action = stash_for_undo("pin", [self.pin], self.profile)
        delete_pin(self.pin, children_mode="delete")
        return restore_undo_action(action)[0]

    def test_the_photo_survives_the_delete_itself(self) -> None:
        """Anchors the rest: SET_NULL, so the photo is detached, not destroyed."""
        stash_for_undo("pin", [self.pin], self.profile)
        delete_pin(self.pin, children_mode="delete")

        self.image.refresh_from_db()
        self.assertIsNone(self.image.pin_id)

    def test_undo_puts_the_photo_back_on_the_pin(self) -> None:
        restored = self._delete_and_undo()

        self.image.refresh_from_db()
        self.assertEqual(self.image.pin_id, restored.pk, "the restored pin came back without its photos")

    def test_a_photo_reattached_elsewhere_is_left_alone(self) -> None:
        """The user's later choice wins over the undo."""
        other = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Elsewhere")
        action = stash_for_undo("pin", [self.pin], self.profile)
        delete_pin(self.pin, children_mode="delete")
        Image.objects.filter(pk=self.image.pk).update(pin=other)

        restore_undo_action(action)

        self.image.refresh_from_db()
        self.assertEqual(self.image.pin_id, other.pk, "undo stole a photo the user had since filed elsewhere")

    def test_a_pin_with_no_photos_restores_cleanly(self) -> None:
        bare = baker.make(Pin, profile=self.profile, location=baker.make(Location), name="Bare")
        action = stash_for_undo("pin", [bare], self.profile)
        delete_pin(bare, children_mode="delete")

        restored = restore_undo_action(action)[0]

        self.assertEqual(Image.objects.filter(pin=restored).count(), 0)

    def test_an_entry_stashed_before_image_ids_existed_still_restores(self) -> None:
        """Payloads written by the previous version have no image_ids key."""
        from urbanlens.dashboard.models.undo import UndoAction

        action = stash_for_undo("pin", [self.pin], self.profile)
        payload = action.payload
        for entry in payload:
            entry.pop("image_ids", None)
        UndoAction.objects.filter(pk=action.pk).update(payload=payload)
        action.refresh_from_db()
        delete_pin(self.pin, children_mode="delete")

        restored = restore_undo_action(action)[0]

        self.assertIsNotNone(restored)
