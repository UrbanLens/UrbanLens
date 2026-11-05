"""The evidence ladder: strong places outrank synthetic ones, and footprints with no parcel become places."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceEvidence, PlaceKind
from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain, ResolvedBoundaries
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
        middle = make_place(PlaceKind.BUILDING, _box(40))
        footprint = make_place(PlaceKind.BUILDING, _box(20))
        Place.objects.filter(pk=footprint.pk).update(evidence=PlaceEvidence.FOOTPRINT)
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), footprint)
        Place.objects.filter(pk=footprint.pk).update(status="superseded")
        self.assertEqual(Place.objects.resolve_for_point(LAT, LON), middle)

    def test_a_smaller_provisional_place_loses_to_a_larger_official_one(self) -> None:
        for evidence in (PlaceEvidence.STANDIN, PlaceEvidence.ANCHORED, PlaceEvidence.PROVISIONAL):
            Place.objects.all().delete()
            official = make_place(PlaceKind.PARCEL, _box(200))
            small = make_place(PlaceKind.BUILDING, _box(5))
            Place.objects.filter(pk=small.pk).update(evidence=evidence)
            self.assertLess(small.area_sqm, official.area_sqm)
            self.assertEqual(Place.objects.resolve_for_point(LAT, LON), official, evidence)

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
        microsoft, overture = _box(10), _box(11, lon=LON + 1 * M_LON)  # true outlines, centroids inside each other
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


class FootprintResolutionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)

    def _footprint(self, half_m: float, *, lat: float = LAT, lon: float = LON) -> Place:
        return provisioning.ensure_footprint_places([("overture", _box(half_m, lon=lon, lat=lat))])[0]

    def test_the_true_outline_is_stored(self) -> None:
        place = self._footprint(10)
        self.assertAlmostEqual(place.area_sqm or 0, 400, delta=20)

    def test_a_pin_on_a_roof_2m_from_a_shed_resolves_to_the_house(self) -> None:
        house = self._footprint(10)
        shed = self._footprint(1.5, lat=LAT + 13 * M_LAT)  # 2.5 m past the house's north edge
        pin = _pin(north_m=9)
        self.assertEqual(Place.objects.resolve_for_point(pin.latitude, pin.longitude), house)
        self.assertNotEqual(house, shed)

    def test_two_sheds_6m_apart_get_two_places(self) -> None:
        places = provisioning.ensure_footprint_places(
            [("overture", _box(1.5)), ("overture", _box(1.5, lat=LAT + 6 * M_LAT))]
        )
        self.assertEqual(len(places), 2)

    def test_a_pin_just_outside_picks_the_nearest_footprint(self) -> None:
        near = self._footprint(5)
        self._footprint(5, lat=LAT + 30 * M_LAT)
        pin = _pin(north_m=9)  # 4 m past the near edge, 16 m from the other
        self.assertEqual(Place.objects.resolve_for_point(pin.latitude, pin.longitude), near)

    def test_nothing_resolves_beyond_the_buffer(self) -> None:
        self._footprint(5)
        pin = _pin(north_m=5 + provisioning.FOOTPRINT_BUFFER_METERS + 3)
        self.assertIsNone(Place.objects.resolve_for_point(pin.latitude, pin.longitude))

    def test_a_parcel_provisioned_later_wins_for_yard_pins(self) -> None:
        footprint = self._footprint(10)
        yard = _pin(north_m=14)  # 4 m past the house: resolved by the buffer
        provisioning.resolution.resolve_location_place(yard)
        yard.refresh_from_db()
        self.assertEqual(yard.place_id, footprint.pk)

        parcel = _box(80)
        _provision(
            yard, property_polygon=parcel, building_polygon=_box(10), building_candidates=[("overture", _box(10))]
        )
        yard.refresh_from_db()
        assert yard.place is not None
        self.assertEqual(yard.place.kind, PlaceKind.PARCEL)
        footprint.refresh_from_db()
        self.assertEqual(footprint.parent_id, yard.place_id)
        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), 1)

    def test_an_official_outline_upgrades_a_footprint_place(self) -> None:
        footprint = self._footprint(10)
        official = _box(9)
        upgraded = provisioning.ensure_footprint_places([("overpass", official)])[0]
        self.assertEqual(upgraded.pk, footprint.pk)
        upgraded.refresh_from_db()
        self.assertEqual(upgraded.evidence, PlaceEvidence.OFFICIAL)
        self.assertAlmostEqual(upgraded.area_sqm or 0, 324, delta=20)

    def test_creation_order_does_not_change_the_result(self) -> None:
        outcomes = []
        for order in ((("overpass", _box(9)), ("overture", _box(10))), (("overture", _box(10)), ("overpass", _box(9)))):
            Place.objects.all().delete()
            for candidate in order:
                provisioning.ensure_footprint_places([candidate])
            only = Place.objects.get()
            outcomes.append((only.evidence, round(only.area_sqm or 0)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], PlaceEvidence.OFFICIAL)

    def test_a_deferring_second_footprint_provider_keeps_the_first_answer(self) -> None:
        existing = self._footprint(3, lat=LAT + 4 * M_LAT)  # makes the pin already resolve
        pin = _pin()
        microsoft = _box(10)
        place = _provision(
            pin,
            building_polygon=microsoft,
            building_candidates=[("microsoft_building_footprints", microsoft)],
            deferred=["overture"],
        )
        self.assertEqual(Place.objects.filter(kind=PlaceKind.BUILDING).count(), 2)
        assert place is not None
        self.assertNotEqual(place.pk, existing.pk)
        self.assertEqual(Place.objects.resolve_for_point(pin.latitude, pin.longitude), place)

    def test_the_chain_keeps_the_first_footprint_when_a_later_source_defers(self) -> None:
        class Answers:
            service_key = "microsoft_building_footprints"
            boundary_kind = "building"

            def get_typed_boundaries(self, latitude, longitude, *, name=None):
                return {"building": _box(10)}

        class Defers:
            service_key = "overture"
            boundary_kind = "building"

            def get_typed_boundaries(self, latitude, longitude, *, name=None):
                raise BoundaryProviderDeferredError("overture", retry_after=60)

        resolved = BoundaryProviderChain(providers=(Answers(), Defers())).get_boundaries(LAT, LON)
        self.assertEqual([key for key, _ in resolved.building_candidates], ["microsoft_building_footprints"])
        self.assertEqual(resolved.deferred, ["overture"])
        self.assertIsNotNone(resolved.building_polygon)

    def test_building_records_carry_evidence_by_source(self) -> None:
        self.assertEqual(
            provisioning.evidence_for_record({"sources": [{"source": "county_gis"}]}), PlaceEvidence.OFFICIAL
        )
        self.assertEqual(
            provisioning.evidence_for_record({"sources": [{"source": "overture"}]}), PlaceEvidence.FOOTPRINT
        )
        self.assertEqual(provisioning.evidence_for_source("county_gis"), PlaceEvidence.OFFICIAL)
        self.assertEqual(provisioning.evidence_for_source("overture_maps"), PlaceEvidence.FOOTPRINT)
