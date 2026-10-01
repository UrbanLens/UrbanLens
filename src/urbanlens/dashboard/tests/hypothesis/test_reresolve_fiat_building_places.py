"""Locations the campus sweep attached by fiat to an outline-less building go back to what containment says."""

from __future__ import annotations

from io import StringIO

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind

_WEST, _SOUTH = -73.93, 41.73


def _square(size: float, *, west: float = _WEST, south: float = _SOUTH) -> MultiPolygon:
    ring = ((west, south), (west + size, south), (west + size, south + size), (west, south + size), (west, south))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class ReresolveFiatBuildingPlacesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parcel = baker.make(Place, kind=PlaceKind.PARCEL, geometry=_square(0.01), area_sqm=1_000_000.0)
        self.outline_less = baker.make(Place, kind=PlaceKind.BUILDING, geometry=None, parent=self.parcel)
        self.stale = baker.make(Location, latitude=_SOUTH + 0.005, longitude=_WEST + 0.005, place=self.outline_less)

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("reresolve_fiat_building_places", *args, stdout=out)
        return out.getvalue()

    def test_a_location_on_an_outline_less_building_goes_back_to_the_parcel_around_it(self) -> None:
        self._run()

        self.stale.refresh_from_db()
        self.assertEqual(self.stale.place_id, self.parcel.pk)

    def test_a_location_containment_cannot_place_is_left_on_no_place(self) -> None:
        nowhere = baker.make(Location, latitude=10.0, longitude=10.0, place=self.outline_less)

        self._run()

        nowhere.refresh_from_db()
        self.assertIsNone(nowhere.place_id)

    def test_a_location_on_a_building_with_an_outline_is_left_alone(self) -> None:
        building = baker.make(
            Place, kind=PlaceKind.BUILDING, geometry=_square(0.001, west=_WEST + 0.008), area_sqm=100.0
        )
        inside = baker.make(Location, latitude=_SOUTH + 0.0005, longitude=_WEST + 0.0085, place=building)

        self._run()

        inside.refresh_from_db()
        self.assertEqual(inside.place_id, building.pk)

    def test_a_dry_run_writes_nothing(self) -> None:
        out = self._run("--dry-run")

        self.stale.refresh_from_db()
        self.assertEqual(self.stale.place_id, self.outline_less.pk)
        self.assertIn(f"location {self.stale.pk}: place {self.outline_less.pk} -> {self.parcel.pk}", out)
