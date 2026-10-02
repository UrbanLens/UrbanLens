"""Migration 0034 puts Locations the campus sweep attached by fiat to an outline-less building back where containment says."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceStatus
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0034_reresolve_fiat_building_places")

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

    def _migrate(self) -> None:
        _MIGRATION._reresolve_fiat_building_places(apps, None)

    def test_a_location_on_an_outline_less_building_goes_back_to_the_parcel_around_it(self) -> None:
        self._migrate()

        self.stale.refresh_from_db()
        self.assertEqual(self.stale.place_id, self.parcel.pk)
        self.assertIsNotNone(self.stale.place_resolved_at)

    def test_smaller_places_resolve_for_point_refuses_are_refused_here_too(self) -> None:
        around_the_point = _square(0.001, west=_WEST + 0.0045, south=_SOUTH + 0.0045)
        implausible_root = baker.make(Place, kind=PlaceKind.PARCEL, geometry=None, area_sqm=50_000_000.0)
        refused = {
            baker.make(
                Place, kind=PlaceKind.PARCEL, status=PlaceStatus.SUPERSEDED, geometry=around_the_point, area_sqm=10.0
            ).pk,
            baker.make(Place, kind=PlaceKind.PARCEL, is_aggregate=True, geometry=around_the_point, area_sqm=20.0).pk,
            baker.make(
                Place, kind=PlaceKind.BUILDING, geometry=around_the_point, area_sqm=5.0, domain_root=implausible_root
            ).pk,
        }
        self.assertEqual(Place.objects.resolve_for_point(self.stale.latitude, self.stale.longitude), self.parcel)

        self._migrate()

        self.stale.refresh_from_db()
        self.assertNotIn(self.stale.place_id, refused)
        self.assertEqual(self.stale.place_id, self.parcel.pk)

    def test_a_location_containment_cannot_place_is_left_on_no_place_and_asked_about_again(self) -> None:
        nowhere = baker.make(Location, latitude=10.0, longitude=10.0, place=self.outline_less)

        self._migrate()

        nowhere.refresh_from_db()
        self.assertIsNone(nowhere.place_id)
        self.assertIsNone(nowhere.place_resolved_at)

    def test_a_location_on_a_building_with_an_outline_is_left_alone(self) -> None:
        building = baker.make(
            Place, kind=PlaceKind.BUILDING, geometry=_square(0.001, west=_WEST + 0.008), area_sqm=100.0
        )
        inside = baker.make(Location, latitude=_SOUTH + 0.0005, longitude=_WEST + 0.0085, place=building)

        self._migrate()

        inside.refresh_from_db()
        self.assertEqual(inside.place_id, building.pk)

    def test_the_parcel_buildings_cache_bounded_by_the_old_place_is_dropped(self) -> None:
        LocationCache.objects.create(location=self.stale, source=PARCEL_BUILDINGS_CACHE_SOURCE, data={"buildings": []})
        LocationCache.objects.create(location=self.stale, source="wikipedia", data={})

        self._migrate()

        self.assertEqual(
            list(LocationCache.objects.filter(location=self.stale).values_list("source", flat=True)), ["wikipedia"]
        )
