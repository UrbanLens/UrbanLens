"""Deleting a markup map is restorable from Undo History, annotations included.

The map row is trivial; the hand-drawn ``PinMarkup`` annotations that cascade with
it are the expensive part. Shares are deliberately NOT restored - the delete severed
those relationships, and undo brings back the owner's work, not other people's
access to it.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo


class MarkupMapUndoTests(TestCase):
    """The markup_map handler round-trips the map and its drawings."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        location = Location.objects.create(latitude=51.2, longitude=-64.8)
        self.pin = Pin.objects.create(profile=self.profile, location=location, name="Annotated place")
        self.markup_map = MarkupMap.objects.create(
            profile=self.profile,
            title="Entry routes",
            pin=self.pin,
            center_latitude=51.2,
            center_longitude=-64.8,
            zoom=17.0,
        )
        self.annotation = PinMarkup.objects.create(
            parent_map=self.markup_map,
            profile=self.profile,
            markup_type="arrow",
            geometry={"points": [[51.2, -64.8], [51.201, -64.801]]},
            label="Side door",
            color="#ff0000",
        )

    def _delete_with_undo(self) -> UndoAction:
        undo_action = stash_for_undo("markup_map", [self.markup_map], self.profile)
        self.markup_map.delete()
        return undo_action

    def test_the_map_and_its_annotations_survive_the_round_trip(self):
        undo_action = self._delete_with_undo()
        self.assertEqual(PinMarkup.objects.count(), 0)  # the cascade took the drawing

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.title, "Entry routes")
        self.assertEqual(restored.zoom, 17.0)
        items = list(restored.items.all())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].markup_type, "arrow")
        self.assertEqual(items[0].label, "Side door")
        self.assertEqual(items[0].geometry, {"points": [[51.2, -64.8], [51.201, -64.801]]})

    def test_the_pin_link_survives(self):
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.pin_id, self.pin.pk)

    def test_a_pin_deleted_since_drops_the_link_not_the_restore(self):
        undo_action = self._delete_with_undo()
        self.pin.delete()

        restored = restore_undo_action(undo_action)[0]

        self.assertIsNone(restored.pin_id)
        self.assertEqual(restored.items.count(), 1)

    def test_shares_are_not_restored(self):
        recipient: Profile = baker.make("auth.User").profile
        MarkupMapShare.objects.create(markup_map=self.markup_map, from_profile=self.profile, to_profile=recipient)
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)[0]

        self.assertFalse(MarkupMapShare.objects.filter(markup_map=restored).exists())

    def test_the_restore_schedules_a_pin_inference_resync_that_sees_the_annotations(self):
        # bulk_create fires no post_save, so without the handler's explicit
        # defer_pin_inference_sync the restored drawing would never be scanned
        # for detected pins - the bulk-write signal guard caught exactly this.
        from unittest import mock

        undo_action = self._delete_with_undo()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            restored = restore_undo_action(undo_action)[0]
            self.assertEqual(restored.items.count(), 1)  # items exist before any callback runs

        with mock.patch("urbanlens.dashboard.services.sharing.map_pin_share_detection.sync_pin_inferences") as sync:
            for callback in callbacks:
                callback()

        synced_maps = {call.args[0].pk for call in sync.call_args_list}
        self.assertIn(restored.pk, synced_maps)

    def test_the_delete_view_stashes(self):
        self.client.force_login(self.profile.user)

        response = self.client.post(f"/dashboard/markup-maps/{self.markup_map.uuid}/delete/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MarkupMap.objects.filter(pk=self.markup_map.pk).exists())
        action = UndoAction.objects.for_profile(self.profile).filter(model_label="markup_map").first()
        self.assertIsNotNone(action, "the delete endpoint did not stash for undo")
        self.assertIn("Entry routes", action.object_repr)
