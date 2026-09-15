"""A radius filter reaches Postgres as a predicate the spatial index can answer, not a distance computed per row.

``distance_lte`` compiles to ``ST_Distance(point, x) <= r``, which no index serves. On the capacity population a 50 m
location lookup planned a parallel scan of all 400k locations, and a pin's nearby layer measured all 21k of the
account's pins. ``dwithin`` compiles to ``ST_DWithin``, which the GiST index on the column answers.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
import json
from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.device_scan.model import WikiDeviceMarker
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.exposure import LocationExposure
from urbanlens.dashboard.services.locations.enrichment import _nearby_density_score
from urbanlens.dashboard.services.memories.photos import find_matching_pin
from urbanlens.dashboard.services.sharing.share_provenance import find_profile_pin_near_location
from urbanlens.dashboard.services.visits.visits import find_nearest_pin

FAR_PINS = 200
SOURCE_ROOT = Path(__file__).resolve().parents[3]


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


class RadiusFiltersUseTheSpatialIndexTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile
        far = Location.objects.bulk_create(
            Location(
                latitude=round(40 + i * 0.01, 6),
                longitude=-100.0,
                point=Point(-100.0, round(40 + i * 0.01, 6), srid=4326),
            )
            for i in range(FAR_PINS)
        )
        Pin.objects.bulk_create(
            Pin(profile=self.profile, location=location, name=f"Far {i}") for i, location in enumerate(far)
        )
        self.near_location = Location.objects.create(latitude=10.0, longitude=20.0)
        self.near_pin = baker.make(Pin, profile=self.profile, location=self.near_location, name="Near")
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE dashboard_locations, dashboard_user_pins")

    def assertRadiusIsIndexable(self, run: Callable[[], Any], *, reads_the_index: bool = False) -> Any:
        with CaptureQueriesContext(connection) as captured:
            result = run()
        statements = [
            query["sql"]
            for query in captured.captured_queries
            if "ST_DWithin(" in query["sql"] or "ST_Distance(" in query["sql"]
        ]
        self.assertTrue(statements, "the call ran no radius query")
        for sql in statements:
            with connection.cursor() as cursor:
                cursor.execute("SET enable_seqscan = off")
                try:
                    cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                    row = cursor.fetchone()
                finally:
                    cursor.execute("RESET enable_seqscan")
            self.assertIsNotNone(row)
            raw = row[0] if row else "[]"
            plan = (raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]
            predicates = [
                node[key]
                for node in _nodes(plan)
                for key in ("Filter", "Index Cond", "Join Filter", "Recheck Cond")
                if key in node
            ]
            self.assertFalse([p for p in predicates if "st_distance(" in p], f"a distance is computed per row: {sql}")
            if reads_the_index:
                self.assertTrue(
                    [node for node in _nodes(plan) if "&&" in node.get("Index Cond", "")],
                    f"the spatial index is not read: {sql}",
                )
        return result

    def test_a_pins_nearby_layer(self) -> None:
        nearby = self.assertRadiusIsIndexable(
            lambda: list(Pin.objects.filter(profile=self.profile).near_point(self.near_location.point, radius_km=5)),
            reads_the_index=True,
        )
        self.assertEqual(nearby, [self.near_pin])

    def test_a_coordinate_within_the_threshold_joins_the_existing_location(self) -> None:
        found = self.assertRadiusIsIndexable(
            lambda: Location.objects.get_nearby_or_create(10.0002, 20.0, threshold_meters=50), reads_the_index=True
        )
        self.assertEqual(found, (self.near_location, False))

    def test_a_coordinate_on_no_place_finds_the_location_standing_there(self) -> None:
        found = self.assertRadiusIsIndexable(
            lambda: Location.objects.get_for_point(10.0002, 20.0), reads_the_index=True
        )
        self.assertEqual(found, self.near_location)

    def test_a_places_density_counts_its_neighbours(self) -> None:
        Location.objects.create(latitude=10.001, longitude=20.0)
        score = self.assertRadiusIsIndexable(lambda: _nearby_density_score(self.near_location), reads_the_index=True)
        self.assertEqual(score, 1)

    def test_a_visit_is_matched_to_the_nearest_pin(self) -> None:
        found = self.assertRadiusIsIndexable(lambda: find_nearest_pin(10.0002, 20.0, self.profile, 100))
        self.assertEqual(found, self.near_pin)

    def test_a_photo_is_matched_to_its_pin(self) -> None:
        found = self.assertRadiusIsIndexable(lambda: find_matching_pin(self.profile, 10.0002, 20.0))
        self.assertEqual(found, self.near_pin)

    def test_a_share_finds_the_recipients_pin_near_the_place(self) -> None:
        shared_place = Location.objects.create(latitude=10.0005, longitude=20.0)
        found = self.assertRadiusIsIndexable(lambda: find_profile_pin_near_location(self.profile.pk, shared_place))
        self.assertEqual(found, self.near_pin)

    def test_exposures_near_a_place(self) -> None:
        exposure = baker.make(LocationExposure, profile=self.profile, location=self.near_location)
        shared_place = Location.objects.create(latitude=10.0005, longitude=20.0)
        found = self.assertRadiusIsIndexable(
            lambda: list(LocationExposure.objects.near(self.profile.pk, shared_place, radius_meters=150))
        )
        self.assertEqual(found, [exposure])

    def test_device_markers_near_a_point(self) -> None:
        now = timezone.now()
        marker = baker.make(
            WikiDeviceMarker, centroid=Point(20.0, 10.0, srid=4326), first_observed_at=now, last_observed_at=now
        )
        found = self.assertRadiusIsIndexable(
            lambda: list(WikiDeviceMarker.objects.near(Point(20.0, 10.0002, srid=4326), 50))
        )
        self.assertEqual(found, [marker])


class NoRadiusFilterComputesDistancesPerRowTests(SimpleTestCase):
    def test_production_code_filters_by_radius_with_dwithin(self) -> None:
        offences = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
                continue
            text = path.read_text(encoding="utf-8")
            if "__distance_lt" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg is not None
                    and node.arg.endswith(("__distance_lt", "__distance_lte"))
                ):
                    offences.append(f"{path.relative_to(SOURCE_ROOT)}:{node.value.lineno}: {node.arg}")
        self.assertEqual(offences, [], "distance_lt(e) cannot use a spatial index; filter with dwithin")
