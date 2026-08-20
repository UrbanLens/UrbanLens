"""Building places follow the nesting REData reports, not a flat parcel list.

REData's reconciled `/parcels/{uuid}/buildings/` reports structure: a coarse
footprint enclosing finer ones becomes their ``parent_ref`` rather than a
duplicate of them (its ``docs/buildings-dedup-spec.md``). `parcel_buildings`
already reads that for display order and counting.

`ensure_building_places` did not: every building was created with
``parent=parcel``, so a Kirkbride block and the wings inside it became
*siblings* whose footprints overlap. The Place tree then asserts two peers
occupy the same ground - which is exactly the ambiguity the reconciliation
exists to remove, and the shape `resolve_locations_in` then has to guess at when
a pin lands inside both.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.services.places.provisioning import ensure_building_places

from .place_helpers import make_place


def _square(x: float, y: float, size: float) -> dict:
    """A GeoJSON polygon, in the shape the building records carry."""
    return {
        "type": "Polygon",
        "coordinates": [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
    }


def _parcel():
    outline = MultiPolygon(Polygon(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0))))
    return make_place(PlaceKind.PARCEL, outline, name="Hospital parcel")


class BuildingPlaceNestingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.parcel = _parcel()

    def _places(self, buildings: list[dict]) -> dict:
        created = ensure_building_places(self.parcel, buildings, provider="redata")
        return {buildings[index]["ref"]: place for index, place in created.items()}

    def test_a_wing_is_nested_under_its_enclosing_block(self) -> None:
        places = self._places(
            [
                {"ref": "osm:way/1", "name": "Kirkbride", "geometry": _square(0.1, 0.1, 0.6), "child_refs": ["cris:1"]},
                {"ref": "cris:1", "name": "North Wing", "parent_ref": "osm:way/1", "geometry": _square(0.15, 0.15, 0.08)},
            ],
        )

        self.assertEqual(places["cris:1"].parent_id, places["osm:way/1"].pk)
        self.assertEqual(places["osm:way/1"].parent_id, self.parcel.pk)

    def test_nesting_deeper_than_one_level_is_followed(self) -> None:
        """A campus block parenting a wing parenting an annex."""
        places = self._places(
            [
                {"ref": "block", "name": "Block", "geometry": _square(0.1, 0.1, 0.6)},
                {"ref": "wing", "name": "Wing", "parent_ref": "block", "geometry": _square(0.15, 0.15, 0.10)},
                {"ref": "annex", "name": "Annex", "parent_ref": "wing", "geometry": _square(0.16, 0.16, 0.03)},
            ],
        )

        self.assertEqual(places["annex"].parent_id, places["wing"].pk)
        self.assertEqual(places["wing"].parent_id, places["block"].pk)
        self.assertEqual(places["block"].parent_id, self.parcel.pk)

    def test_children_are_created_even_when_listed_before_their_parent(self) -> None:
        """The list order is REData's, and it does not promise parents come first."""
        places = self._places(
            [
                {"ref": "wing", "name": "Wing", "parent_ref": "block", "geometry": _square(0.15, 0.15, 0.08)},
                {"ref": "block", "name": "Block", "geometry": _square(0.1, 0.1, 0.6)},
            ],
        )

        self.assertEqual(len(places), 2)
        self.assertEqual(places["wing"].parent_id, places["block"].pk)

    def test_a_parent_outside_the_list_falls_back_to_the_parcel(self) -> None:
        """Its parent may have been dropped as off-property; the child is not lost."""
        places = self._places([{"ref": "cris:1", "name": "Orphan", "parent_ref": "gone", "geometry": _square(0.2, 0.2, 0.1)}])

        self.assertEqual(places["cris:1"].parent_id, self.parcel.pk)

    def test_a_parent_ref_cycle_does_not_hang_and_loses_no_building(self) -> None:
        """Two buildings each claiming the other as parent.

        Resolved by repeated passes rather than recursion precisely so this
        terminates - and both still become places, because the import already
        told the user it would create them.
        """
        places = self._places(
            [
                {"ref": "a", "name": "A", "parent_ref": "b", "geometry": _square(0.15, 0.15, 0.08)},
                {"ref": "b", "name": "B", "parent_ref": "a", "geometry": _square(0.45, 0.45, 0.08)},
            ],
        )

        self.assertEqual(sorted(places), ["a", "b"])
        self.assertEqual(places["a"].parent_id, self.parcel.pk)
        self.assertEqual(places["b"].parent_id, self.parcel.pk)

    def test_a_flat_list_still_parents_everything_to_the_parcel(self) -> None:
        """Overpass-shaped records carry no ref/parent_ref at all."""
        created = ensure_building_places(
            self.parcel,
            [{"name": "Shed", "geometry": _square(0.2, 0.2, 0.1)}, {"name": "Barn", "geometry": _square(0.5, 0.5, 0.1)}],
            provider="",
        )

        self.assertEqual(len(created), 2)
        for place in created.values():
            self.assertEqual(place.parent_id, self.parcel.pk)

    def test_no_parcel_creates_nothing(self) -> None:
        """A building place with no parcel has no domain to join."""
        self.assertEqual(ensure_building_places(None, [{"ref": "a", "geometry": _square(0.2, 0.2, 0.1)}]), {})


class DistinctRecordsStayDistinctTests(TestCase):
    """A stable id the provider gave is stronger evidence than overlapping shapes.

    `find_matching_place` falls back to mutual centroid containment when no
    place carries the record's id - sensible for providers that publish no id,
    and how two sources' views of one parcel get merged. But it applied to
    *identified* records too, and nested buildings are exactly where that goes
    wrong: an L-shaped block can contain a wing's centroid while the wing
    contains the block's. The two would then collapse into one place, undoing
    the reconciliation REData did to keep them apart.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.parcel = _parcel()

    def test_two_ids_from_one_provider_never_merge_on_geometry(self) -> None:
        # Deliberately mutually centroid-containing: an L-shaped block and a
        # wing tucked into its corner produce this in the real world.
        created = ensure_building_places(
            self.parcel,
            [
                {"ref": "block", "name": "Block", "geometry": _square(0.10, 0.10, 0.40)},
                {"ref": "wing", "name": "Wing", "geometry": _square(0.28, 0.28, 0.06)},
            ],
            provider="redata",
        )

        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0].pk, created[1].pk, "two reconciled buildings must not become one place")

    def test_a_record_without_an_id_can_still_match_by_geometry(self) -> None:
        """The fallback is the whole reason two sources' parcels merge; keep it."""
        first = ensure_building_places(self.parcel, [{"name": "Shed", "geometry": _square(0.20, 0.20, 0.10)}], provider="")
        second = ensure_building_places(self.parcel, [{"name": "Shed", "geometry": _square(0.201, 0.201, 0.10)}], provider="")

        self.assertEqual(first[0].pk, second[0].pk)

    def test_the_same_id_still_matches_itself(self) -> None:
        first = ensure_building_places(self.parcel, [{"ref": "block", "name": "Block", "geometry": _square(0.10, 0.10, 0.40)}], provider="redata")
        second = ensure_building_places(self.parcel, [{"ref": "block", "name": "Block", "geometry": _square(0.10, 0.10, 0.41)}], provider="redata")

        self.assertEqual(first[0].pk, second[0].pk)
