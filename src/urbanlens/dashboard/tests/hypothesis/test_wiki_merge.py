"""Tests for automatic wiki parent/child nesting on boundary containment (services.wiki_merge).

Two independently-created community wikis - a campus and one of its own
buildings - should read as parent/child once both have a real property
boundary, without asking anyone. Covers both directions reconciliation checks
(a wiki finding a bigger container; a wiki absorbing smaller wikis already
inside it), the circle-fallback guard that must never drive a merge, and that
reconciliation only ever touches ``parent_wiki`` - never Pins, Articles, or
edit history, since ``Wiki.location`` never moves.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.wiki_merge import (
    reconcile_wiki_nesting,
    reconcile_wiki_nesting_for_location,
    wiki_property_polygon,
)

_coord_counter = 0

#: Sizes (degrees, half-width of the square boundary) used throughout this
#: file, largest first - each roughly 5x the next, so a small wiki's own
#: boundary can never accidentally reach back and "contain" its container.
CAMPUS_SIZE = 0.05
WING_SIZE = 0.01
BUILDING_SIZE = 0.001
TINY_SIZE = 0.0001

#: Default offset (degrees) between a wiki and whatever it's placed "near" -
#: must sit strictly between the two boundary sizes in play, so the container
#: really contains the point while the (much smaller) nested wiki's own
#: boundary does not reach back and contain the container's point in turn.
#: Works for any BUILDING_SIZE/TINY_SIZE-vs-CAMPUS_SIZE pairing; the WING_SIZE
#: pairings need their own explicit, tighter offset (see call sites).
_NEAR_OFFSET = 0.01


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 43.0 + _coord_counter * 0.5)
    kwargs.setdefault("longitude", -76.0 - _coord_counter * 0.5)
    return baker.make(Location, google_place=None, **kwargs)


def _square(latitude: float, longitude: float, size: float) -> MultiPolygon:
    """A MultiPolygon square of ``size`` degrees a side, centred on a coordinate."""
    return MultiPolygon(
        Polygon(
            (
                (longitude - size, latitude - size),
                (longitude + size, latitude - size),
                (longitude + size, latitude + size),
                (longitude - size, latitude + size),
                (longitude - size, latitude - size),
            ),
        ),
        srid=4326,
    )


def _make_wiki_with_boundary(*, size: float | None, near: Wiki | None = None, near_offset: float = _NEAR_OFFSET) -> Wiki:
    """A wiki whose location-default PROPERTY boundary is a real polygon of the given size.

    ``size=None`` leaves the boundary row unset entirely (no ``generated_at``),
    exercising ``resolve_for_wiki``'s circle fallback. ``near`` places the new
    wiki ``near_offset`` degrees from another wiki's own coordinate (never
    identical - Location's (lat, lng) pair must be unique) - a "building"
    passed the "campus" it should nest inside of, for example. Callers pairing
    two boundary sizes closer together than the default (e.g. WING_SIZE as the
    container) must pass a tighter ``near_offset`` themselves - see the
    module-level size/offset comments.
    """
    if near is not None:
        location = baker.make(
            Location,
            latitude=float(near.location.latitude) + near_offset,
            longitude=float(near.location.longitude) + near_offset,
            google_place=None,
        )
    else:
        location = _make_location()
    wiki = baker.make(Wiki, location=location, name=f"Wiki {location.pk}")
    if size is not None:
        Boundary.objects.create(
            location=location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square(float(location.latitude), float(location.longitude), size),
        )
    return wiki


class WikiPropertyPolygonTests(TestCase):
    def test_a_real_generated_polygon_is_returned(self) -> None:
        wiki = _make_wiki_with_boundary(size=0.01)
        self.assertIsNotNone(wiki_property_polygon(wiki))

    def test_no_boundary_at_all_falls_back_to_none(self) -> None:
        """No Boundary row means resolve_for_wiki's circle fallback - never a real polygon."""
        wiki = _make_wiki_with_boundary(size=None)
        self.assertIsNone(wiki_property_polygon(wiki))


class ReconcileWikiNestingTests(TestCase):
    """Both directions of automatic merging."""

    def test_a_new_campus_absorbs_a_pre_existing_building_wiki(self) -> None:
        """Building created first (direction 1 finds nothing yet); campus created and
        reconciled second finds the building inside it (direction 2)."""
        building = _make_wiki_with_boundary(size=BUILDING_SIZE)
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE, near=building)

        merged = reconcile_wiki_nesting(campus)

        self.assertEqual(merged, 1)
        building.refresh_from_db()
        self.assertEqual(building.parent_wiki_id, campus.pk)

    def test_a_new_building_finds_its_pre_existing_campus(self) -> None:
        """Campus already exists; the new building's own reconciliation (direction 1)
        finds it as a container."""
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE)
        building = _make_wiki_with_boundary(size=BUILDING_SIZE, near=campus)

        merged = reconcile_wiki_nesting(building)

        self.assertEqual(merged, 1)
        campus.refresh_from_db()
        building.refresh_from_db()
        self.assertIsNone(campus.parent_wiki_id)
        self.assertEqual(building.parent_wiki_id, campus.pk)

    def test_the_tightest_fitting_container_wins_not_the_outermost(self) -> None:
        """A building inside a wing inside a campus becomes the wing's child, not the campus's.

        One reconciliation call handles both hops: direction 1 (on wing) finds
        campus as wing's own container, and direction 2 (also on wing, using
        wing's own polygon) finds building already sitting inside it - both in
        the single call triggered by wing's own boundary generation. Real
        usage never reconciles two different wikis' Python objects in the same
        process without reloading, so this mirrors that rather than calling
        reconcile a second time on a stale in-memory ``building``.
        """
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE)
        # WING_SIZE (0.01) is close enough to the default offset (0.01) that a
        # wider offset is needed here so wing's own boundary can't reach back
        # and "contain" campus in turn - see the module-level offset comment.
        wing = _make_wiki_with_boundary(size=WING_SIZE, near=campus, near_offset=0.02)
        building = _make_wiki_with_boundary(size=BUILDING_SIZE, near=wing, near_offset=0.005)

        merged = reconcile_wiki_nesting(wing)

        self.assertEqual(merged, 2)
        building.refresh_from_db()
        wing.refresh_from_db()
        self.assertEqual(wing.parent_wiki_id, campus.pk)
        self.assertEqual(building.parent_wiki_id, wing.pk, "the building's immediate parent is the wing, not the campus")

    def test_unrelated_wikis_are_never_merged(self) -> None:
        a = _make_wiki_with_boundary(size=WING_SIZE)
        b = _make_wiki_with_boundary(size=WING_SIZE)

        reconcile_wiki_nesting(a)
        reconcile_wiki_nesting(b)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.parent_wiki_id)
        self.assertIsNone(b.parent_wiki_id)

    def test_a_circle_fallback_boundary_never_absorbs_anything(self) -> None:
        """No real polygon on the 'campus' side - must not silently claim a nearby wiki."""
        bare = _make_wiki_with_boundary(size=None)
        nearby = _make_wiki_with_boundary(size=TINY_SIZE, near=bare, near_offset=0.001)

        merged = reconcile_wiki_nesting(bare)

        self.assertEqual(merged, 0)
        nearby.refresh_from_db()
        self.assertIsNone(nearby.parent_wiki_id)

    def test_a_childs_own_missing_boundary_does_not_block_it_being_absorbed(self) -> None:
        """Direction 1 only needs the *candidate parent's* real boundary - a child with
        no boundary of its own (only the fallback circle) can still be absorbed by one."""
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE)
        bare = _make_wiki_with_boundary(size=None, near=campus)

        merged = reconcile_wiki_nesting(bare)

        self.assertEqual(merged, 1)
        bare.refresh_from_db()
        self.assertEqual(bare.parent_wiki_id, campus.pk)

    def test_an_already_nested_wiki_is_never_re_matched(self) -> None:
        """Direction 2 only ever considers root wikis - idempotence guard."""
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE)
        building = _make_wiki_with_boundary(size=BUILDING_SIZE, near=campus)
        reconcile_wiki_nesting(campus)
        building.refresh_from_db()
        self.assertEqual(building.parent_wiki_id, campus.pk)

        # A second reconciliation of the campus must not error or double-merge.
        merged_again = reconcile_wiki_nesting(campus)
        self.assertEqual(merged_again, 0)

    def test_a_child_wiki_keeps_its_own_children_when_absorbed(self) -> None:
        """Multi-level nesting: absorbing a wiki must not disturb its own subtree."""
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE)
        wing = _make_wiki_with_boundary(size=WING_SIZE, near=campus, near_offset=0.02)
        room = baker.make(Wiki, location=_make_location(), parent_wiki=wing, name="Room")

        reconcile_wiki_nesting(campus)

        wing.refresh_from_db()
        room.refresh_from_db()
        self.assertEqual(wing.parent_wiki_id, campus.pk)
        self.assertEqual(room.parent_wiki_id, wing.pk, "the wing's own child must be untouched")

    def test_merging_logs_a_wiki_edit_on_the_parent(self) -> None:
        building = _make_wiki_with_boundary(size=BUILDING_SIZE)
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE, near=building)

        reconcile_wiki_nesting(campus)

        edit = WikiEdit.objects.get(wiki=campus)
        self.assertIn("child_wiki_merged", edit.changes)
        self.assertIsNone(edit.editor, "an automatic merge has no human editor")

    def test_merging_never_touches_the_childs_own_location(self) -> None:
        """The whole point of merging via parent_wiki alone: Pins/Articles/comments
        never need to move, since Wiki.location is untouched."""
        building = _make_wiki_with_boundary(size=BUILDING_SIZE)
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE, near=building)
        original_location_id = building.location_id

        reconcile_wiki_nesting(campus)

        building.refresh_from_db()
        self.assertEqual(building.location_id, original_location_id)


class ReconcileWikiNestingForLocationTests(TestCase):
    def test_a_location_without_a_wiki_is_a_no_op(self) -> None:
        location = _make_location()
        self.assertEqual(reconcile_wiki_nesting_for_location(location), 0)

    def test_a_location_with_a_wiki_delegates_to_reconcile(self) -> None:
        building = _make_wiki_with_boundary(size=BUILDING_SIZE)
        campus = _make_wiki_with_boundary(size=CAMPUS_SIZE, near=building)

        merged = reconcile_wiki_nesting_for_location(campus.location)

        self.assertEqual(merged, 1)
        building.refresh_from_db()
        self.assertEqual(building.parent_wiki_id, campus.pk)


class GenerateLocationBoundariesIntegrationTests(TestCase):
    """generate_location_boundaries is the single choke point reconciliation hooks into."""

    def test_reconciliation_runs_after_boundary_generation(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import generate_location_boundaries

        location = _make_location()
        baker.make(Wiki, location=location, name="Some Wiki")
        with (
            patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain") as mock_chain_cls,
            patch("urbanlens.dashboard.services.wiki_merge.reconcile_wiki_nesting_for_location") as mock_reconcile,
        ):
            mock_chain_cls.return_value.get_boundaries.return_value.polygon_for.return_value = None
            generate_location_boundaries(location)
        mock_reconcile.assert_called_once_with(location)
