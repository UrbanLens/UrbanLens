"""Deleting a label is restorable from Undo History, including what the cascade took.

The label row is the least of it: deleting a label also severs its place in the
hierarchy (both directions of the ``parents`` self-M2M) and its assignment to every
pin carrying it - and a label's order decides which icon a pin draws on the map, so
those assignments are visible state.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo


class LabelUndoTests(TestCase):
    """The label handler round-trips fields, hierarchy, and pin assignments."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.parent = Label.objects.create(profile=self.profile, name="Industrial", kind="tag", order=5)
        self.label = Label.objects.create(
            profile=self.profile, name="Mills", kind="tag", order=3, icon="factory", color="#aa2200", keywords="mill, textile",
        )
        self.child = Label.objects.create(profile=self.profile, name="Textile mills", kind="tag", order=1)
        self.label.parents.add(self.parent)
        self.child.parents.add(self.label)

        location = Location.objects.create(latitude=50.5, longitude=-65.5)
        self.pin = Pin.objects.create(profile=self.profile, location=location, name="Old mill")
        self.pin.labels.add(self.label)

    def _delete_with_undo(self, labels=None) -> UndoAction:
        labels = labels or [self.label]
        undo_action = stash_for_undo("label", labels, self.profile)
        for label in labels:
            label.delete()
        return undo_action

    def test_fields_survive_the_round_trip(self):
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.name, "Mills")
        self.assertEqual(restored.kind, "tag")
        self.assertEqual(restored.icon, "factory")
        self.assertEqual(restored.color, "#aa2200")
        self.assertEqual(restored.order, 3)
        self.assertEqual(restored.keywords, "mill, textile")

    def test_the_pin_gets_its_label_back(self):
        undo_action = self._delete_with_undo()
        self.assertEqual(self.pin.labels.count(), 0)  # the cascade stripped it

        restored = restore_undo_action(undo_action)[0]

        self.assertIn(restored.pk, self.pin.labels.values_list("pk", flat=True))

    def test_both_directions_of_the_hierarchy_relink(self):
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)[0]

        self.assertIn(self.parent.pk, restored.parents.values_list("pk", flat=True))
        self.assertIn(restored.pk, self.child.parents.values_list("pk", flat=True))

    def test_a_parent_deleted_since_is_skipped_not_fatal(self):
        undo_action = self._delete_with_undo()
        self.parent.delete()

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.parents.count(), 0)

    def test_a_pin_deleted_since_is_skipped_not_fatal(self):
        undo_action = self._delete_with_undo()
        self.pin.delete()

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.pins.count(), 0)

    def test_a_bulk_delete_relinks_hierarchy_within_the_batch(self):
        # Deleting parent and child together must restore the link between their
        # two *new* rows, not point at the dead pks.
        undo_action = self._delete_with_undo([self.parent, self.label])

        restored = restore_undo_action(undo_action)
        by_name = {label.name: label for label in restored}

        self.assertEqual(
            list(by_name["Mills"].parents.values_list("pk", flat=True)),
            [by_name["Industrial"].pk],
        )

    def test_a_name_reused_since_does_not_block(self):
        # Label has no unique constraints; the merge tool handles duplicates. A
        # restore refusing here would be stricter than the app itself is.
        undo_action = self._delete_with_undo()
        Label.objects.create(profile=self.profile, name="Mills", kind="tag")

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(Label.objects.filter(profile=self.profile, name="Mills").count(), 2)

    def test_the_single_delete_view_stashes(self):
        self.client.force_login(self.profile.user)

        response = self.client.post(f"/dashboard/tags/{self.label.pk}/delete/")

        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(Label.objects.filter(pk=self.label.pk).exists())
        action = UndoAction.objects.for_profile(self.profile).filter(model_label="label").first()
        self.assertIsNotNone(action, "the single-delete endpoint did not stash for undo")
        self.assertIn("Mills", action.object_repr)

    def test_the_bulk_delete_view_stashes(self):
        self.client.force_login(self.profile.user)

        response = self.client.post(
            "/dashboard/tags/bulk-delete/",
            data={"ids": [self.label.pk, self.child.pk]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        action = UndoAction.objects.for_profile(self.profile).filter(model_label="label").first()
        self.assertIsNotNone(action, "the bulk-delete endpoint did not stash for undo")

    def test_an_empty_bulk_delete_stashes_nothing(self):
        self.client.force_login(self.profile.user)

        self.client.post(
            "/dashboard/tags/bulk-delete/",
            data={"ids": [999999]},
            content_type="application/json",
        )

        self.assertFalse(UndoAction.objects.for_profile(self.profile).filter(model_label="label").exists())
