"""Deleting a saved filter is restorable from Undo History, tint included.

`_RESTORABLE_FIELDS` used to list `("name", "icon", "criteria", "order")` -
omitting `color`/`opacity`, so undoing a deleted filter brought it back
untinted. `test_undo_round_trip`'s generic sweep can't see this class of bug:
`model_bakery` leaves a field with an explicit Django-level default (`color`'s
`default=""`, `opacity`'s `default=100`) unset rather than fuzzing it, so
"before" and "after" are both the same default and nothing looks lost. This
builds the filter with an explicit non-default color/opacity instead.

See PROBLEMS.md, "undoing a deleted saved filter drops its colour and
opacity".
"""

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
