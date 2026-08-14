"""New bulk writes on models with signal receivers must be reviewed, not silent.

``update()``, ``bulk_update()`` and ``bulk_create()`` fire no ``post_save``/``post_delete``.
This codebase maintains a lot of derived state in those receivers - denormalized
``Pin.last_visited``, cached map payloads, smart-list membership, calendar pushes - so
each such write is a place derived state can silently stop tracking.

Three separate bugs of exactly this shape were found in consecutive audit chunks, in three
different models:

- label reorder never invalidated the cached map payload, though label ``order`` decides
  which label supplies a pin's icon;
- trip activity reorder never pushed to the synced calendar;
- pin merge never recomputed the survivor's ``last_visited``, leaving it months stale.

They recur because a bulk write is reached for precisely when a loop feels slow, which is
precisely when many rows change.

This does **not** assert that every site invalidates. Bypassing a signal is often exactly
right - marking a notification read must *not* re-fire ``enqueue_native_push``, and the
majority of the sites below are correct for reasons like that. What it asserts is that the
set does not grow unreviewed: a new entry means someone should decide which case it is.

To add a site: run the test, read the receivers on that model, decide whether the derived
state they maintain matters for your write, then add the key. The decision is the point;
the list is just what makes skipping it visible.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase

#: Sites reviewed as of the 2026-08-14 audit. See the module docstring before extending.
REVIEWED = {
    # Reorder/edit paths that DO invalidate explicitly (verified).
    "dashboard/controllers/labels.py::Label",
    "dashboard/controllers/organize.py::Label",
    "dashboard/external_api/views_labels_bulk.py::Label",
    "dashboard/services/trips/trip_activities.py::TripActivity",
    # Deliberate bypasses: the receiver's work is unwanted for this write.
    "dashboard/controllers/visit_suggestions.py::NotificationLog",
    "dashboard/services/sharing/pin_sharing.py::NotificationLog",
    "dashboard/controllers/location_wiki.py::Wiki",
    "dashboard/services/wiki/wiki_merge.py::Wiki",
    # Touch-only writes (bump `updated`), where re-firing would be circular.
    "dashboard/controllers/labels.py::Pin",
    "dashboard/services/labels/customization.py::Pin",
    # Reviewed in the merge sweep - receivers key off something the write does not touch.
    "dashboard/services/pins/pin_merge.py::MarkupMap",
    "dashboard/services/pins/pin_merge.py::PinLink",
    "dashboard/services/pins/pin_merge.py::PinMarkup",
    "dashboard/services/pins/pin_merge.py::TripActivity",
    # Bulk retype: pin_type is absent from the map payload, no smart filter reads it,
    # and refit_child_boundaries_on_save early-returns unless position changed.
    "dashboard/services/locations/site_scope.py::Pin",
    "dashboard/services/locations/site_scope.py::Wiki",
    # Deliberate bypass of a *transient* state (a pin briefly parented to itself),
    # commented as such at the site and resolved via deferred_ids.
    "dashboard/services/pins/pin_edit.py::Pin",
    # Writes a field nothing derived reads (not in the map payload, no smart filter).
    "dashboard/controllers/memories.py::Pin",
    # Copies a list into a trip; queues the calendar push explicitly after bulk_create.
    "dashboard/services/pins/pin_list_trip.py::TripActivity",
    "dashboard/services/undo/handlers/markup_map.py::PinMarkup",
    "dashboard/services/wiki/wiki_creation.py::Pin",
    "dashboard/tasks.py::Wiki",
}

_RECEIVER = re.compile(r"@receiver\(\s*(?:post_save|post_delete)\s*,\s*sender=([A-Za-z_][\w.]*)")
_BULK_WRITE = re.compile(r"(\w+)\.objects\.(?:filter|exclude)\([^\n]*\)\.update\(|(\w+)\.objects\.bulk_(?:update|create)\(")


def _source_files(root: Path):
    for path in root.rglob("*.py"):
        if "/tests/" not in str(path) and "/migrations/" not in str(path):
            yield path


class BulkWritesOnReceiverModelsTests(SimpleTestCase):
    """Static scan - no database, no fixtures."""

    def _scan(self) -> set[str]:
        root = Path(settings.BASE_DIR) if hasattr(settings, "BASE_DIR") else Path(__file__).resolve().parents[4]
        root = root if (root / "dashboard").exists() else Path(__file__).resolve().parents[3]

        with_receivers: set[str] = set()
        for path in _source_files(root):
            for match in _RECEIVER.finditer(path.read_text(errors="replace")):
                with_receivers.add(match.group(1).split(".")[-1])

        found: set[str] = set()
        for path in _source_files(root):
            for line in path.read_text(errors="replace").splitlines():
                match = _BULK_WRITE.search(line)
                if match:
                    model = match.group(1) or match.group(2)
                    if model in with_receivers:
                        found.add(f"{path.relative_to(root)}::{model}")
        return found

    def test_no_unreviewed_bulk_writes_on_models_with_receivers(self) -> None:
        found = self._scan()

        self.assertTrue(found, "scan found nothing at all - the pattern or the root path is wrong")

        new = found - REVIEWED
        self.assertFalse(
            new,
            "Bulk write(s) on a model whose signal receivers maintain derived state:\n  "
            + "\n  ".join(sorted(new))
            + "\n\nupdate()/bulk_update()/bulk_create() fire no post_save. Read that model's "
            "receivers, decide whether the state they maintain matters for this write "
            "(invalidate explicitly if so), then add the key to REVIEWED in this file.",
        )

    def test_the_reviewed_list_has_no_stale_entries(self) -> None:
        """A key that no longer matches anything is a moved or deleted site - drop it."""
        stale = REVIEWED - self._scan()
        self.assertFalse(stale, f"REVIEWED names sites that no longer exist: {sorted(stale)}")


class BulkWriteScanSanityTests(SimpleTestCase):
    """The scan must actually be capable of failing."""

    def test_the_pattern_matches_a_known_bulk_write_form(self) -> None:
        for sample in (
            "        Label.objects.bulk_update(labels, ['order'])",
            "    Pin.objects.filter(pk=pin.pk).update(x=1)",
            "    items = TripActivity.objects.bulk_create([",
        ):
            self.assertTrue(_BULK_WRITE.search(sample), f"pattern missed: {sample!r}")

    def test_the_pattern_ignores_an_ordinary_save(self) -> None:
        self.assertIsNone(_BULK_WRITE.search("    pin.save(update_fields=['updated'])"))
