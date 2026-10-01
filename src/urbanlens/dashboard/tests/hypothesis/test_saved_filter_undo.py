"""Deleting a saved filter is restorable from Undo History, tint included."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo


class SavedFilterUndoTests(TestCase):
    """The saved-filter handler round-trips every restorable field."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.saved_filter = baker.make(
            SavedFilter,
            profile=self.profile,
            name="Abandoned mills",
            icon="factory",
            color="#673AB7",
            opacity=42,
            criteria={"status": ["abandoned"]},
            order=2,
        )

    def _delete_with_undo(self) -> UndoAction:
        undo_action = stash_for_undo("saved_filter", [self.saved_filter], self.profile)
        self.saved_filter.delete()
        return undo_action

    def test_fields_survive_the_round_trip(self):
        undo_action = self._delete_with_undo()

        restored = restore_undo_action(undo_action)[0]

        self.assertEqual(restored.name, "Abandoned mills")
        self.assertEqual(restored.icon, "factory")
        self.assertEqual(restored.color, "#673AB7")
        self.assertEqual(restored.opacity, 42)
        self.assertEqual(restored.criteria, {"status": ["abandoned"]})
        self.assertEqual(restored.order, 2)
