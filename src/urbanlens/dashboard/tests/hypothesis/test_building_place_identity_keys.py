"""Building places are identified by keys that mean the same building everywhere, and only those.

A building number is local to its parcel ("Building 9" exists on countless campuses), and an OSM record
has a global id of its own; both were filed under the caller's provider namespace as if global."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.services.places.provisioning import ensure_building_places

from .place_helpers import make_place


def _parcel(x: float):
    outline = MultiPolygon(Polygon(((x, 0.0), (x + 1.0, 0.0), (x + 1.0, 1.0), (x, 1.0), (x, 0.0))))
    return make_place(PlaceKind.PARCEL, outline, name=f"parcel {x}")


class BuildingPlaceIdentityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion

    def test_the_same_building_number_on_two_parcels_is_two_places(self) -> None:
        first = ensure_building_places(
            _parcel(0.0), [{"name": "Building 9", "building_number": "9"}], provider="redata"
        )
        second = ensure_building_places(
            _parcel(5.0), [{"name": "Building 9", "building_number": "9"}], provider="redata"
        )

        self.assertNotEqual(first[0].pk, second[0].pk)

    def test_the_same_building_number_on_one_parcel_is_one_place(self) -> None:
        parcel = _parcel(0.0)
        first = ensure_building_places(parcel, [{"name": "Building 9", "building_number": "9"}], provider="redata")
        again = ensure_building_places(parcel, [{"name": "Building 9", "building_number": "9"}], provider="redata")

        self.assertEqual(first[0].pk, again[0].pk)

    def test_footprintless_osm_buildings_each_get_a_place_keyed_like_redatas_osm_refs(self) -> None:
        records = [
            {
                "name": "",
                "building_number": "",
                "latitude": 0.5,
                "longitude": 0.1 * index,
                "osm_id": 100 + index,
                "osm_type": "way",
                "source": "osm",
            }
            for index in range(8)
        ]

        places = ensure_building_places(_parcel(0.0), records, provider="redata")

        self.assertEqual(len({place.pk for place in places.values()}), 8)
        self.assertEqual(places[0].provider_key, "osm:way/100")

    def test_redata_later_naming_the_same_osm_building_finds_its_place(self) -> None:
        parcel = _parcel(0.0)
        from_osm = ensure_building_places(
            parcel, [{"osm_id": 7, "osm_type": "way", "source": "osm"}], provider="redata"
        )
        from_redata = ensure_building_places(parcel, [{"ref": "osm:way/7", "name": "Kirkbride"}], provider="redata")

        self.assertEqual(from_osm[0].pk, from_redata[0].pk)
