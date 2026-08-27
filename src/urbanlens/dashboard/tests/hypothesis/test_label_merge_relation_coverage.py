"""Merging labels must move every relation that carries content.

``merge_labels`` consumes the source labels and deletes them, so any relation
FK'd to Label that it does not move is cascade-deleted - the same shape as the
pin merge, which was silently destroying albums and map overlays.

Label survives that check, with one relation deliberately not moved:

``LabelCustomization`` holds a profile's display overrides (name/icon/colour)
*for the label being deleted*. It carries no content of its own, and moving it
onto the target would be actively wrong: the target would silently adopt styling
the user chose for a different label, and would overwrite any customization the
target already had. Letting it cascade is the correct outcome, so it is listed
here as an exemption rather than "handled".

The rest of the check is the same completeness arm the pin merge grew: naming the
relations that exist today would not catch the next one added.
"""

from __future__ import annotations

import inspect
import re

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.labels import merge as label_merge

#: Relations intentionally left to cascade when a source label is deleted.
_EXPECTED_CASCADING: dict[str, str] = {
    "LabelCustomization": (
        "display overrides for the deleted label itself; moving them would restyle the target "
        "with settings chosen for a different label"
    ),
}


def _cascade_relations() -> set[str]:
    return {
        rel.related_model.__name__
        for rel in Label._meta.related_objects
        if getattr(getattr(rel.field.remote_field, "on_delete", None), "__name__", "") == "CASCADE"
    }


class LabelMergeRelationCoverageTests(SimpleTestCase):
    def test_the_scan_finds_relations(self) -> None:
        """Guards the check below against passing on an empty set."""
        self.assertGreaterEqual(len(_cascade_relations()), 2)

    def test_every_cascading_relation_is_moved_or_exempt(self) -> None:
        source = inspect.getsource(label_merge)

        unhandled = sorted(
            name
            for name in _cascade_relations()
            if name not in _EXPECTED_CASCADING and not re.search(rf"\b{name}\b", source)
        )

        self.assertEqual(
            unhandled,
            [],
            "these relations are destroyed when a label is merged away - move them or exempt them with a reason",
        )

    def test_each_exemption_states_why_losing_it_is_correct(self) -> None:
        thin = [name for name, reason in _EXPECTED_CASCADING.items() if len((reason or "").strip()) < 30]

        self.assertEqual(thin, [])

    def test_exemptions_are_real_relations(self) -> None:
        """A stale exemption would hide a relation that later starts mattering."""
        stale = sorted(set(_EXPECTED_CASCADING) - _cascade_relations())

        self.assertEqual(stale, [])
