"""``revert_edit_fields``'s boundary-restore branch.

A wiki's community-drawn Boundary polygon is edited through a separate path
from ``apply_wiki_edit`` (see ``external_api/views_wiki.py``'s
``WikiBoundaryUpdateView.post``), which records the change as a ``WikiEdit``
keyed ``"boundary_<type>"`` (or the legacy ``"bounding_box"``) holding WKT
strings rather than plain field values. ``revert_edit_fields`` has a whole
separate code path for these keys - parsing WKT, creating/updating/deleting
the ``Boundary`` row, and its own version of the conflict check - that had no
test coverage before this file. Mutation testing
(``bin/run_mutation_tests.sh --results``) shows nearly every mutant in
``revert_edit_fields`` surviving, concentrated in exactly this branch.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.wiki.wiki_edits import revert_edit_fields


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


_OLD = _square(-74.0, 40.0, 0.001)
_NEW = _square(-74.0, 40.0, 0.002)
_OTHER = _square(-74.0, 40.0, 0.003)


class WikiBoundaryRevertTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Mill")

    def test_reverting_restores_the_prior_polygon(self) -> None:
        row = baker.make(
            "dashboard.Boundary",
            wiki=self.wiki,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon=_NEW,
        )
        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"boundary_property": {"from": _OLD.wkt, "to": _NEW.wkt}})

        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(skipped, [])
        self.assertIn("boundary_property", revert_changes)
        row.refresh_from_db()
        self.assertTrue(row.polygon.equals(_OLD))

    def test_reverting_a_newly_drawn_boundary_deletes_the_row(self) -> None:
        """A "from": null edit created the boundary from nothing - reverting removes it."""
        row = baker.make(
            "dashboard.Boundary",
            wiki=self.wiki,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon=_NEW,
        )

        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"boundary_property": {"from": None, "to": _NEW.wkt}})
        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(skipped, [])
        self.assertIn("boundary_property", revert_changes)
        self.assertFalse(Boundary.objects.filter(pk=row.pk).exists())

    def test_reverting_a_cleared_boundary_recreates_the_row(self) -> None:
        """A "to": null edit had deleted the row - reverting brings it back."""
        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"boundary_property": {"from": _OLD.wkt, "to": None}})

        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(skipped, [])
        self.assertIn("boundary_property", revert_changes)
        row = Boundary.objects.row_for_wiki(self.wiki, BoundaryType.PROPERTY)
        self.assertIsNotNone(row)
        self.assertTrue(row.polygon.equals(_OLD))

    def test_a_boundary_changed_again_since_is_left_alone(self) -> None:
        """The conflict check: a third polygon was drawn after this edit - reverting must not clobber it."""
        row = baker.make(
            "dashboard.Boundary",
            wiki=self.wiki,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon=_OTHER,
        )
        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"boundary_property": {"from": _OLD.wkt, "to": _NEW.wkt}})

        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(skipped, ["boundary_property"])
        self.assertEqual(revert_changes, {})
        row.refresh_from_db()
        self.assertTrue(row.polygon.equals(_OTHER), "a later, unrelated change was clobbered by the revert")

    def test_the_legacy_bounding_box_key_targets_the_property_boundary(self) -> None:
        row = baker.make(
            "dashboard.Boundary",
            wiki=self.wiki,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon=_NEW,
        )
        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"bounding_box": {"from": _OLD.wkt, "to": _NEW.wkt}})

        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(skipped, [])
        self.assertIn(
            "bounding_box",
            revert_changes,
            "the legacy key name is preserved in the diff, not rewritten to boundary_property",
        )
        row.refresh_from_db()
        self.assertTrue(row.polygon.equals(_OLD))

    def test_an_unrecognized_boundary_type_is_silently_skipped(self) -> None:
        """Not a conflict - just a key this code doesn't know how to act on (never
        recorded by the current writer, but a defensive case worth locking in)."""
        edit = baker.make(WikiEdit, wiki=self.wiki, changes={"boundary_bogus": {"from": _OLD.wkt, "to": _NEW.wkt}})

        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertEqual(revert_changes, {})
        self.assertEqual(skipped, [])

    def test_legacy_latitude_longitude_keys_are_skipped_without_error(self) -> None:
        """Coordinates stopped being editable, but old WikiEdit rows recording
        them must still revert (skipping just that field) instead of crashing."""
        self.wiki.name = "New Mill"
        self.wiki.save(update_fields=["name"])
        edit = baker.make(
            WikiEdit,
            wiki=self.wiki,
            changes={"latitude": {"from": "40.0", "to": "40.1"}, "name": {"from": "Mill", "to": "New Mill"}},
        )

        # revert_edit_fields mutates `wiki` in place and leaves persisting it
        # to the caller (see save_edited_fields) - asserted on the in-memory
        # instance it was given, not a fresh read of the still-unsaved row.
        revert_changes, skipped = revert_edit_fields(self.location, self.wiki, edit)

        self.assertNotIn("latitude", revert_changes)
        self.assertNotIn("latitude", skipped)
        self.assertIn("name", revert_changes)
        self.assertEqual(self.wiki.name, "Mill")
