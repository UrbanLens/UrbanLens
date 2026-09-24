"""A location that moves onto a different place must not keep data derived from the old one.

HRSH's campus location kept a parcel_buildings cache of 13,018 OSM buildings fetched inside a
legacy 102.9 km² outline. The boundary repair re-homed the location onto its real 0.47 km² parcel,
but the building sweep and the floorplan editor kept reading the town-sized list.
"""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind, PlaceStatus
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.places import resolution

LAT, LNG = 41.73328, -73.92812


def _box(size: float) -> MultiPolygon:
    ring = (
        (LNG - size, LAT - size),
        (LNG + size, LAT - size),
        (LNG + size, LAT + size),
        (LNG - size, LAT + size),
        (LNG - size, LAT - size),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class PlaceChangeInvalidatesCachesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.oversized = baker.make(Place, kind=PlaceKind.PARCEL, geometry=_box(0.015), area_sqm=8_300_000.0)
        self.location = baker.make(Location, latitude=LAT, longitude=LNG, place=self.oversized)
        LocationCache.set(
            self.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"provider": "osm", "buildings": [{"name": "far away"}]}
        )
        LocationCache.set(self.location, "wikipedia", {"title": "Hudson River State Hospital"})

    def test_moving_onto_another_place_drops_the_place_scoped_cache(self) -> None:
        Place.objects.filter(pk=self.oversized.pk).update(status=PlaceStatus.SUPERSEDED)
        real = baker.make(Place, kind=PlaceKind.PARCEL, geometry=_box(0.003), area_sqm=470_000.0)

        resolved = resolution.resolve_location_place(self.location)

        self.assertEqual(resolved, real)
        self.assertFalse(
            LocationCache.objects.filter(location=self.location, source=PARCEL_BUILDINGS_CACHE_SOURCE).exists(),
            "the building list fetched inside the old outline survived the move onto the real parcel",
        )

    def test_caches_that_do_not_depend_on_the_place_survive_the_move(self) -> None:
        Place.objects.filter(pk=self.oversized.pk).update(status=PlaceStatus.SUPERSEDED)
        baker.make(Place, kind=PlaceKind.PARCEL, geometry=_box(0.003), area_sqm=470_000.0)

        resolution.resolve_location_place(self.location)

        self.assertTrue(LocationCache.objects.filter(location=self.location, source="wikipedia").exists())

    def test_an_unchanged_place_keeps_the_cache(self) -> None:
        """Anti-vacuity: re-resolving onto the same place must not throw away a good building list."""
        resolution.resolve_location_place(self.location)

        self.assertTrue(
            LocationCache.objects.filter(location=self.location, source=PARCEL_BUILDINGS_CACHE_SOURCE).exists()
        )
