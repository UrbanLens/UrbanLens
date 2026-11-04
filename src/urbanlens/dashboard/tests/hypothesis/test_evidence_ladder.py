"""The evidence ladder: strong places outrank synthetic ones, and footprints with no parcel become places."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceEvidence, PlaceKind
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries
from urbanlens.dashboard.services.places import provisioning

from .place_helpers import make_place

LAT, LON = 42.65, -73.75
#: One metre of latitude / longitude at LAT, roughly.
M_LAT = 1 / 111_320
M_LON = M_LAT / 0.7361


def _box(half_m: float, lon: float = LON, lat: float = LAT) -> MultiPolygon:
    """A square centred on (lon, lat), ``half_m`` metres each way."""
    dx, dy = half_m * M_LON, half_m * M_LAT
    return MultiPolygon(
        Polygon(
            (
                (lon - dx, lat - dy),
                (lon + dx, lat - dy),
                (lon + dx, lat + dy),
                (lon - dx, lat + dy),
                (lon - dx, lat - dy),
            ),
            srid=4326,
        ),
        srid=4326,
    )


def _pin(north_m: float = 0.0) -> Location:
    return baker.make(Location, latitude=LAT + north_m * M_LAT, longitude=LON)


def _provision(location: Location, **resolved) -> Place | None:
    with patch(
        "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
        return_value=ResolvedBoundaries(**resolved),
    ):
        return provisioning.provision_places_for_coordinate(location, detect_splits=False)


class EvidenceResolutionOrderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)

    def test_a_provisional_place_never_beats_a_larger_official_place(self) -> None:
        official = make_place(PlaceKind.PARCEL, _box(100))
        provisional = make_place(PlaceKind.BUILDING, _box(10))
        Place.objects.filter(pk=provisional.pk).update(evidence=PlaceEvidence.PROVISIONAL)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), official)

    def test_a_provisional_place_never_beats_a_larger_footprint_place(self) -> None:
        footprint = make_place(PlaceKind.BUILDING, _box(100))
        Place.objects.filter(pk=footprint.pk).update(evidence=PlaceEvidence.FOOTPRINT)
        for synthetic in (PlaceEvidence.STANDIN, PlaceEvidence.ANCHORED, PlaceEvidence.PROVISIONAL):
            place = make_place(PlaceKind.BUILDING, _box(10))
            Place.objects.filter(pk=place.pk).update(evidence=synthetic)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), footprint)

    def test_smallest_wins_within_the_strong_group(self) -> None:
        make_place(PlaceKind.PARCEL, _box(100))
        footprint = make_place(PlaceKind.BUILDING, _box(20))
        Place.objects.filter(pk=footprint.pk).update(evidence=PlaceEvidence.FOOTPRINT)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), footprint)

    def test_a_synthetic_place_resolves_when_nothing_strong_does(self) -> None:
        synthetic = make_place(PlaceKind.BUILDING, _box(10))
        Place.objects.filter(pk=synthetic.pk).update(evidence=PlaceEvidence.PROVISIONAL)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), synthetic)

    def test_existing_places_default_to_official(self) -> None:
        self.assertEqual(make_place(PlaceKind.PARCEL, _box(50)).evidence, PlaceEvidence.OFFICIAL)


class FootprintWithoutParcelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)

    def test_a_footprint_with_no_parcel_becomes_a_resolvable_place(self) -> None:
        footprint = _box(10)
        location = _pin()
        place = _provision(
            location, building_polygon=footprint, building_candidates=[("microsoft_building_footprints", footprint)]
        )
        self.assertIsNotNone(place)
        assert place is not None
        self.assertEqual(
            (place.kind, place.evidence, place.parent_id), (PlaceKind.BUILDING, PlaceEvidence.FOOTPRINT, None)
        )
        self.assertTrue(place.is_domain_root)
        location.refresh_from_db()
        self.assertEqual(location.place_id, place.pk)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), place)

    def test_a_pin_8m_outside_a_footprint_with_no_parcel_resolves_to_it(self) -> None:
        footprint = _box(10)  # edge at 10 m north of centre
        outside = _pin(north_m=18)
        place = _provision(
            outside, building_polygon=footprint, building_candidates=[("microsoft_building_footprints", footprint)]
        )
        assert place is not None
        outside.refresh_from_db()
        self.assertEqual(outside.place_id, place.pk)
        self.assertEqual(Place.objects.resolve_for_point(outside.latitude, outside.longitude), place)

    def test_a_pin_8m_outside_a_footprint_does_not_resolve_to_it_when_a_parcel_exists(self) -> None:
        footprint = _box(10)
        parcel = _box(60)
        outside = _pin(north_m=18)
        _provision(
            outside,
            property_polygon=parcel,
            building_polygon=footprint,
            building_candidates=[("microsoft_building_footprints", footprint)],
        )
        outside.refresh_from_db()
        assert outside.place is not None
        self.assertEqual(outside.place.kind, PlaceKind.PARCEL)
        building = Place.objects.get(kind=PlaceKind.BUILDING)
        self.assertEqual(building.evidence, PlaceEvidence.FOOTPRINT)
        self.assertEqual(building.parent_id, outside.place_id)
        self.assertFalse(
            building.geometry.contains(Point(float(outside.longitude), float(outside.latitude), srid=4326))
        )

    def test_microsoft_and_overture_outlines_of_one_building_reconcile(self) -> None:
        microsoft, overture = _box(10), _box(11, lon=LON + 1 * M_LON)
        location = _pin()
        _provision(
            location,
            building_polygon=microsoft,
            building_candidates=[("microsoft_building_footprints", microsoft), ("overture", overture)],
        )
        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), 1)

    def test_distinct_buildings_stay_distinct(self) -> None:
        near, far = _box(8), _box(8, lat=LAT + 60 * M_LAT)
        places = provisioning.ensure_footprint_places([("overture", near), ("overture", far)])
        self.assertEqual(len(places), 2)

    def test_an_implausibly_large_footprint_is_refused(self) -> None:
        huge = _box(2000)
        self.assertEqual(provisioning.ensure_footprint_places([("overture", huge)]), [])
        self.assertFalse(Place.objects.exists())

    def test_an_official_outline_is_not_buffered_or_overwritten_by_a_footprint(self) -> None:
        official = _box(10)
        place = provisioning.ensure_footprint_places([("overpass", official)])[0]
        self.assertEqual(place.evidence, PlaceEvidence.OFFICIAL)
        again = provisioning.ensure_footprint_places([("overture", _box(10))])[0]
        self.assertEqual(again.pk, place.pk)
        again.refresh_from_db()
        self.assertEqual(again.evidence, PlaceEvidence.OFFICIAL)
        self.assertAlmostEqual(again.area_sqm or 0, place.area_sqm or 0, delta=1)

    def test_the_footprint_is_grown_by_the_buffer(self) -> None:
        place = provisioning.ensure_footprint_places([("overture", _box(10))])[0]
        grown = (20 + 2 * provisioning.FOOTPRINT_BUFFER_METERS) ** 2
        self.assertAlmostEqual(place.area_sqm or 0, grown, delta=grown * 0.2)
        self.assertGreater(place.area_sqm or 0, 20 * 20 * 1.5)

    def test_ensure_building_places_without_a_parcel_keeps_the_footprints(self) -> None:
        outline = _box(10)
        created = provisioning.ensure_building_places(
            None,
            [
                {
                    "ref": "a",
                    "geometry": {"type": "Polygon", "coordinates": [list(map(list, outline[0].coords[0]))]},
                    "sources": [{"source": "overture"}],
                }
            ],
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].evidence, PlaceEvidence.FOOTPRINT)
