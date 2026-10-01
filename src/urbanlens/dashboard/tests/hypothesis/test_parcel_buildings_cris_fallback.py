"""With REData's building list out of reach, the Overpass fallback roster still carries CRIS's inventory.

Reproduces the HRSH courtyard pin: REData's Dutchess budget was spent, the fallback found 8 OSM footprints on the
campus, and "BLDG 45/MORTUARY & LAB (1896)" - one of 42 CRIS buildings on the parcel, none of which OSM has
footprinted - never became a building pin.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.plugins.builtin.parcel_buildings import (
    countable_buildings,
    fetch_parcel_buildings,
    merge_cris_buildings,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_SOURCE_RATE_LIMITED,
    PropertyRecordsUnavailableError,
)

_CAMPUS = MultiPolygon(Polygon.from_bbox((-73.934, 41.730, -73.923, 41.737)), srid=4326)


def _footprint(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


def _osm(osm_id: int, west: float, south: float, east: float, north: float, name: str = "") -> dict:
    return {
        "name": name,
        "building_number": "",
        "latitude": (south + north) / 2,
        "longitude": (west + east) / 2,
        "osm_id": osm_id,
        "osm_type": "way",
        "source": "osm",
        "geometry": _footprint(west, south, east, north),
    }


def _cris(usn: str, name: str, latitude: float, longitude: float) -> dict:
    return {
        "provider": "ny_cris",
        "resource_type": "building",
        "name": name,
        "external_id": usn,
        "source_latitude": latitude,
        "source_longitude": longitude,
        "attributes": {"USNNum": usn, "USNName": name},
    }


_BLDG45 = _cris("02714.000106", "BLDG 45/MORTUARY & LAB (1896)", 41.733016, -73.926380)
_OFF_PARCEL = _cris("02714.000457", "BLDG 145/GARAGE (1945) - NON-CONTRIBUTING", 41.733850, -73.905338)


class MergeTests(SimpleTestCase):
    def test_a_cris_building_no_footprint_covers_becomes_its_own_record(self) -> None:
        merged = merge_cris_buildings([], [_BLDG45], _CAMPUS)
        self.assertEqual(
            [(b["name"], b["ref"]) for b in merged], [("BLDG 45/MORTUARY & LAB (1896)", "cris:02714.000106")]
        )
        self.assertEqual(merged[0]["source"], "cris")

    def test_a_cris_building_off_the_parcel_is_left_out(self) -> None:
        self.assertEqual(merge_cris_buildings([], [_OFF_PARCEL], _CAMPUS), [])

    def test_one_cris_point_in_an_unnamed_footprint_names_it(self) -> None:
        footprint = _osm(1, -73.9266, 41.7328, -73.9262, 41.7332)
        merged = merge_cris_buildings([footprint], [_BLDG45], _CAMPUS)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["name"], "BLDG 45/MORTUARY & LAB (1896)")
        self.assertEqual(merged[0]["osm_id"], 1)

    def test_a_footprint_over_several_cris_points_is_their_envelope(self) -> None:
        envelope = _osm(2, -73.9270, 41.7325, -73.9255, 41.7335, name="Service Block")
        other = _cris("02714.000107", "BLDG 46/SHED (1900)", 41.7330, -73.9258)
        merged = merge_cris_buildings([envelope], [_BLDG45, other], _CAMPUS)
        by_ref = {b["ref"]: b for b in merged}
        self.assertEqual(by_ref["osm:way/2"]["child_refs"], ["cris:02714.000106", "cris:02714.000107"])
        self.assertEqual(by_ref["cris:02714.000106"]["parent_ref"], "osm:way/2")
        self.assertEqual(len(countable_buildings(merged)), 2)

    def test_a_named_footprint_keeps_its_own_name(self) -> None:
        footprint = _osm(3, -73.9266, 41.7328, -73.9262, 41.7332, name="Mortuary")
        self.assertEqual(merge_cris_buildings([footprint], [_BLDG45], _CAMPUS)[0]["name"], "Mortuary")


class FallbackRosterTests(TestCase):
    def test_the_fallback_roster_includes_cris_buildings_in_new_york(self) -> None:
        parcel = Place.objects.create(kind=PlaceKind.PARCEL, geometry=_CAMPUS)
        Place.objects.filter(pk=parcel.pk).update(domain_root=parcel.pk)
        location = baker.make(Location, latitude=41.73266, longitude=-73.92736)
        Location.objects.filter(pk=location.pk).update(place=parcel)
        location.refresh_from_db()
        module = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"
        with (
            mock.patch(
                f"{module}.lookup_parcel_uuid",
                side_effect=PropertyRecordsUnavailableError(REASON_SOURCE_RATE_LIMITED, "spent"),
            ),
            mock.patch(f"{module}.lookup_cultural_resources", return_value=[_BLDG45, _OFF_PARCEL]),
            mock.patch("urbanlens.dashboard.services.geo.geo_boundary.state_boundary") as state,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overpass.OverpassGateway.buildings_within",
                return_value=[_osm(1019627590, -73.9258, 41.7342, -73.9254, 41.7346)],
            ),
        ):
            state.return_value.contains.return_value = True
            result = fetch_parcel_buildings(location)
        self.assertEqual(result["provider"], "osm")
        names = {b["name"] for b in result["buildings"]}
        self.assertIn("BLDG 45/MORTUARY & LAB (1896)", names)
        self.assertNotIn("BLDG 145/GARAGE (1945) - NON-CONTRIBUTING", names)
        self.assertEqual(len(result["buildings"]), 2)
