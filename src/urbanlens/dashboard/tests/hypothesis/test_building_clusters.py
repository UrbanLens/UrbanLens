"""One marker per physical building, however many records describe it."""

from __future__ import annotations

from itertools import combinations
import json

from django.contrib.gis.geos import GEOSGeometry, Point

from hypothesis import given, settings as hypothesis_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.queryset import quantize_coordinate
from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between
from urbanlens.dashboard.services.pins.building_clusters import (
    SweptBuilding,
    cluster_buildings,
    distinct_building_count,
    marker_point,
    match_clusters,
)
from urbanlens.dashboard.tests.hypothesis.building_fixtures import offset, record, rect, ring


def _geos(geometry: dict) -> GEOSGeometry:
    return GEOSGeometry(json.dumps(geometry), srid=4326)


def _rounded(latitude: float, longitude: float) -> tuple[float, float]:
    return float(quantize_coordinate(latitude, "latitude")), float(quantize_coordinate(longitude, "longitude"))


class ClusterBuildingsTests(SimpleTestCase):
    def test_an_unresolved_overlap_is_one_building_not_two_and_not_none(self) -> None:
        records = [
            record("osm:way/2", 0, 0, geometry=rect(0, 0, 30, 20), overlap_refs=["cris:2"]),
            record("cris:2", 0, 12, geometry=rect(0, 12, 30, 20), overlap_refs=["osm:way/2"], name="Laundry"),
        ]

        clusters = cluster_buildings(records)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].refs, {"osm:way/2", "cris:2"})
        self.assertEqual(
            clusters[0].name, "Laundry", "a named member names the building even when it is not the representative"
        )

    def test_two_sources_tracing_one_footprint_are_one_building(self) -> None:
        """The Overpass fallback reconciles nothing, so two outlines of one block arrive as two records."""
        records = [
            record("osm:way/1", 0, 0, geometry=rect(0, 0, 40, 20)),
            record("msft:1", 1, 3, geometry=rect(1, 3, 40, 20)),
        ]

        self.assertEqual(len(cluster_buildings(records)), 1)

    def test_a_point_left_inside_a_sibling_footprint_stays_its_own_building(self) -> None:
        """REData merges a footprint with the one point inside it unless both come from one source - then they are two."""
        records = [
            record("county_gis:1", 0, 0, geometry=rect(0, 0, 80, 20)),
            record("county_gis:2", 0, 35, name="Ward block"),
        ]

        self.assertEqual(len(cluster_buildings(records)), 2)

    def test_points_within_the_match_radius_are_one_building_and_farther_ones_are_not(self) -> None:
        near = cluster_buildings([record("cris:1", 0, 0), record("cris:2", 0, 10)])
        far = cluster_buildings([record("cris:1", 0, 0), record("cris:2", 0, 30)])

        self.assertEqual(len(near), 1)
        self.assertEqual(len(far), 2)

    def test_a_row_of_close_outbuildings_does_not_chain_into_one(self) -> None:
        """Each only compares against a representative, so three sheds 12 m apart stay two markers, not one."""
        clusters = cluster_buildings([record("cris:1", 0, 0), record("cris:2", 0, 12), record("cris:3", 0, 24)])

        self.assertEqual(len(clusters), 2)

    def test_a_contained_building_never_merges_into_its_container(self) -> None:
        records = [
            record("osm:way/3", 0, 0, geometry=rect(0, 0, 60, 60), child_refs=["cris:3a", "cris:3b"]),
            record("cris:3a", 0, -15, parent_ref="osm:way/3", name="Chapel"),
            record("cris:3b", 0, 15, parent_ref="osm:way/3", name="Garage"),
        ]

        clusters = cluster_buildings(records)

        self.assertEqual([cluster.depth for cluster in clusters], [0, 1, 1])
        self.assertTrue(all(cluster.parent is clusters[0] for cluster in clusters[1:]))
        self.assertEqual({cluster.name for cluster in clusters[1:]}, {"Chapel", "Garage"})
        self.assertEqual(distinct_building_count(clusters), 2, "an envelope over two buildings is two buildings")

    def test_a_parent_ref_cycle_loses_no_record(self) -> None:
        records = [record("cris:1", 0, 0, parent_ref="cris:2"), record("cris:2", 0, 50, parent_ref="cris:1")]

        clusters = cluster_buildings(records)

        self.assertEqual({ref for cluster in clusters for ref in cluster.refs}, {"cris:1", "cris:2"})

    def test_a_record_with_no_coordinate_is_left_out(self) -> None:
        clusters = cluster_buildings([{"ref": "cris:1", "is_on_property": True}, record("cris:2", 0, 0)])

        self.assertEqual([cluster.refs for cluster in clusters], [{"cris:2"}])


class MarkerPointTests(SimpleTestCase):
    def test_a_concave_footprint_gets_a_marker_on_the_building(self) -> None:
        """A U-shaped ward block's centroid is in its courtyard, which is not the building."""
        u_shape = ring([(0, 0), (0, 60), (60, 60), (60, 40), (10, 40), (10, 20), (60, 20), (60, 0)])
        building = record("osm:way/1", 0, 0, geometry=u_shape)
        self.assertFalse(_geos(u_shape).contains(Point(building["longitude"], building["latitude"], srid=4326)))

        latitude, longitude = marker_point(building)

        self.assertTrue(_geos(u_shape).intersects(Point(longitude, latitude, srid=4326)))

    def test_a_building_straddling_the_lot_line_gets_a_marker_on_the_property(self) -> None:
        boundary = _geos(rect(0, 0, 100, 100))
        building = record("osm:way/1", 0, 55, geometry=rect(0, 55, 30, 20))

        latitude, longitude = marker_point(building, boundary)

        point = Point(longitude, latitude, srid=4326)
        self.assertTrue(boundary.intersects(point))
        self.assertTrue(_geos(building["geometry"]).intersects(point))

    def test_a_point_record_keeps_its_own_point(self) -> None:
        building = record("cris:1", 5, 5)

        self.assertEqual(marker_point(building), (building["latitude"], building["longitude"]))


_SPOTS = st.lists(
    st.tuples(st.integers(min_value=-150, max_value=150), st.integers(min_value=-150, max_value=150), st.booleans()),
    min_size=2,
    max_size=14,
)


class ClusterSpacingPropertyTests(SimpleTestCase):
    @hypothesis_settings(max_examples=60, deadline=None)
    @given(_SPOTS)
    def test_no_two_sibling_markers_stand_within_the_match_radius(self, spots: list[tuple[int, int, bool]]) -> None:
        """Whatever the records, the markers placed for one level are never two pins for one building."""
        records = [
            record(f"src:{index}", north, east, geometry=rect(north, east, 12, 8) if has_footprint else None)
            for index, (north, east, has_footprint) in enumerate(spots)
        ]

        clusters = cluster_buildings(records)

        self.assertEqual(
            sum(len(cluster.members) for cluster in clusters), len(records), "every record is placed exactly once"
        )
        for first, second in combinations(clusters, 2):
            distance = meters_between(
                *_rounded(first.latitude, first.longitude), *_rounded(second.latitude, second.longitude)
            )
            self.assertGreaterEqual(distance, BUILDING_MATCH_METERS)


class MatchClustersTests(SimpleTestCase):
    def test_an_earlier_marker_on_the_exact_point_is_recognised_whatever_the_ref_now_says(self) -> None:
        clusters = cluster_buildings([record("cris:1", 0, 0), record("cris:2", 0, 40)])
        swept = [SweptBuilding(clusters[1].latitude, clusters[1].longitude, ref="since-renamed")]

        matched, unmatched = match_clusters(clusters, swept)

        self.assertEqual(matched, {1: swept[0]})
        self.assertEqual(unmatched, [])

    def test_a_marker_on_a_chapel_inside_an_envelope_belongs_to_the_chapel(self) -> None:
        clusters = cluster_buildings(
            [
                record("osm:way/3", 0, 0, geometry=rect(0, 0, 60, 60), child_refs=["cris:3a"]),
                record("cris:3a", 0, -20, parent_ref="osm:way/3"),
            ]
        )
        chapel_pin = SweptBuilding(*offset(0, -19))

        matched, _unmatched = match_clusters(clusters, [chapel_pin])

        self.assertEqual(matched, {1: chapel_pin})

    def test_a_marker_by_a_buildings_own_point_is_that_building_not_its_larger_neighbours(self) -> None:
        clusters = cluster_buildings(
            [
                record("county_gis:1", 0, 0, geometry=rect(0, 0, 80, 20)),
                record("county_gis:2", 0, 35, name="Ward block"),
            ]
        )
        ward_pin = SweptBuilding(*offset(0, 33))

        matched, _unmatched = match_clusters(clusters, [ward_pin])

        self.assertEqual(matched, {1: ward_pin})

    def test_one_marker_stands_for_one_building(self) -> None:
        clusters = cluster_buildings([record("cris:1", 0, 0), record("cris:2", 0, 20)])
        between = SweptBuilding(*offset(0, 10))

        matched, _unmatched = match_clusters(clusters, [between])

        self.assertEqual(len(matched), 1)

    def test_a_far_marker_matches_nothing(self) -> None:
        clusters = cluster_buildings([record("cris:1", 0, 0)])
        far = SweptBuilding(*offset(0, 80))

        matched, unmatched = match_clusters(clusters, [far])

        self.assertEqual(matched, {})
        self.assertEqual(unmatched, [far])


class SweptBuildingTests(SimpleTestCase):
    def test_round_trips_through_its_stored_shape(self) -> None:
        swept = SweptBuilding(41.7, -73.9, ref="cris:1")

        self.assertEqual(SweptBuilding.from_json(swept.to_json()), swept)

    def test_a_malformed_entry_is_ignored(self) -> None:
        for value in (None, "x", {"latitude": "nope", "longitude": 1}, {"longitude": 1}):
            self.assertIsNone(SweptBuilding.from_json(value))
