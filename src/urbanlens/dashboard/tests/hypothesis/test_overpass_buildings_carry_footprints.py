"""OSM buildings found inside a property carry their footprints, not just a centre point.

The footprint is what a building place, a child pin's outline and the floorplan editor's seeded walls
are made from; the parcel-buildings fallback used to ask Overpass for centres only."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.gis.geos import Polygon

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.pins.pin_restructure import building_footprint

_PARCEL = Polygon(
    ((-73.930, 41.731), (-73.920, 41.731), (-73.920, 41.736), (-73.930, 41.736), (-73.930, 41.731)), srid=4326
)


def _ring(west: float, south: float, east: float, north: float) -> list[dict[str, float]]:
    return [
        {"lat": lat, "lon": lon}
        for lon, lat in ((west, south), (east, south), (east, north), (west, north), (west, south))
    ]


_WAY = {
    "type": "way",
    "id": 11,
    "tags": {"building": "yes", "name": "Kirkbride"},
    "bounds": {"minlat": 41.732, "minlon": -73.926, "maxlat": 41.733, "maxlon": -73.924},
    "geometry": _ring(-73.926, 41.732, -73.924, 41.733),
}
_RELATION = {
    "type": "relation",
    "id": 22,
    "tags": {"building": "yes", "type": "multipolygon"},
    "bounds": {"minlat": 41.734, "minlon": -73.928, "maxlat": 41.735, "maxlon": -73.927},
    "members": [{"type": "way", "role": "outer", "geometry": _ring(-73.928, 41.734, -73.927, 41.735)}],
}
_NO_GEOMETRY = {
    "type": "way",
    "id": 33,
    "tags": {"building": "yes"},
    "bounds": {"minlat": 41.7330, "minlon": -73.9220, "maxlat": 41.7332, "maxlon": -73.9218},
}


class BuildingsWithinTests(SimpleTestCase):
    def _buildings(self, elements: list[dict]) -> list[dict]:
        with patch.object(OverpassGateway, "elements_for_query", return_value=elements) as query:
            buildings = OverpassGateway().buildings_within(_PARCEL)
        self.query = query.call_args.args[0]
        return buildings

    def test_a_way_carries_its_footprint(self) -> None:
        (building,) = self._buildings([_WAY])

        footprint = building_footprint(building)
        self.assertIsNotNone(footprint)
        self.assertAlmostEqual(footprint.area, 0.002 * 0.001, places=10)
        self.assertEqual(building["name"], "Kirkbride")

    def test_a_multipolygon_relation_carries_its_outer_ring(self) -> None:
        (building,) = self._buildings([_RELATION])

        self.assertTrue(building_footprint(building).contains(building_footprint(building).centroid))

    def test_the_marker_stays_at_the_centre_overpass_used_to_report(self) -> None:
        (building,) = self._buildings([_WAY])

        self.assertAlmostEqual(building["latitude"], 41.7325)
        self.assertAlmostEqual(building["longitude"], -73.925)

    def test_a_multi_part_relation_keeps_the_whole_elements_centre(self) -> None:
        """Its footprint is only the largest part, but `out center` centred the marker on all of it."""
        relation = {
            "type": "relation",
            "id": 44,
            "tags": {"building": "yes", "type": "multipolygon"},
            "bounds": {"minlat": 41.732, "minlon": -73.929, "maxlat": 41.735, "maxlon": -73.924},
            "members": [
                {"type": "way", "role": "outer", "geometry": _ring(-73.929, 41.732, -73.926, 41.735)},
                {"type": "way", "role": "outer", "geometry": _ring(-73.9245, 41.7345, -73.924, 41.735)},
            ],
        }
        (building,) = self._buildings([relation])

        self.assertAlmostEqual(building["latitude"], 41.7335)
        self.assertAlmostEqual(building["longitude"], -73.9265)

    def test_an_element_without_geometry_still_yields_a_point(self) -> None:
        (building,) = self._buildings([_NO_GEOMETRY])

        self.assertIsNone(building_footprint(building))
        self.assertAlmostEqual(building["latitude"], 41.7331)

    def test_overpass_is_asked_for_geometry_and_relation_members(self) -> None:
        """`tags` verbosity drops a relation's members, and with them every relation-mapped footprint."""
        self._buildings([])
        output = self.query.splitlines()[-1]
        self.assertIn("geom", output)
        self.assertIn("body", output)

    def test_an_outer_ring_split_across_ways_is_stitched_not_closed_on_itself(self) -> None:
        west, south, east, north = -73.928, 41.734, -73.927, 41.735
        halves = [
            [{"lat": south, "lon": west}, {"lat": south, "lon": east}, {"lat": north, "lon": east}],
            [{"lat": north, "lon": east}, {"lat": north, "lon": west}, {"lat": south, "lon": west}],
        ]
        relation = {**_RELATION, "members": [{"type": "way", "role": "outer", "geometry": half} for half in halves]}

        (building,) = self._buildings([relation])

        self.assertAlmostEqual(building_footprint(building).area, 0.001 * 0.001, places=12)
