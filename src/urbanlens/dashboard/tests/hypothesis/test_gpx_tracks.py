"""Tests for services.import_formats.gpx_tracks.gpx_tracks_to_routes().

gpx.py deliberately ignores <trk>/<rte> content when producing pins (see its
module docstring); this module is the counterpart that turns that same
content into Route candidates instead. All tests require the database, since
gpx_tracks_to_routes() builds real (unsaved) Route model instances whose
`profile` FK is validated against a real Profile row.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.routes.model import Route, RouteSource
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.import_formats.gpx_tracks import (
    RawTrackPoint,
    detect_dwells_and_create_visits,
    gpx_tracks_to_routes,
)
from urbanlens.dashboard.services.import_formats.route_geometry import simplify_and_measure

_SAMPLE_DATA_DIR = Path(__file__).resolve().parents[5] / "sample_data"
_PIN_LAT = 40.0
_PIN_LNG = -74.0

_GPX_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  {body}
</gpx>
"""

_TRACK_WITH_TIMESTAMPS = """<trk>
  <name>Evening walk</name>
  <trkseg>
    <trkpt lat="40.000" lon="-73.000"><time>2024-01-01T10:00:00Z</time></trkpt>
    <trkpt lat="40.001" lon="-73.001"><time>2024-01-01T10:05:00Z</time></trkpt>
    <trkpt lat="40.002" lon="-73.002"><time>2024-01-01T10:10:00Z</time></trkpt>
  </trkseg>
</trk>
"""

_ROUTE_NO_TIMESTAMPS = """<rte>
  <name>Planned loop</name>
  <rtept lat="41.000" lon="-72.000" />
  <rtept lat="41.001" lon="-72.001" />
</rte>
"""


def _gpx_bytes(body: str) -> bytes:
    return _GPX_TEMPLATE.format(body=body).encode("utf-8")


class GpxTracksToRoutesTests(TestCase):
    """gpx_tracks_to_routes() converts <trk>/<rte> content into unsaved Route candidates."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_single_track_produces_one_route(self):
        parsed = gpx_tracks_to_routes(_gpx_bytes(_TRACK_WITH_TIMESTAMPS), self.profile, "walk.gpx")

        self.assertEqual(len(parsed), 1)
        route = parsed[0].route
        self.assertEqual(route.name, "Evening walk")
        self.assertEqual(route.source, RouteSource.GPX_TRACK)
        self.assertEqual(route.source_filename, "walk.gpx")
        self.assertEqual(route.raw_point_count, 3)
        self.assertIsNotNone(route.started_at)
        self.assertIsNotNone(route.ended_at)
        self.assertLess(route.started_at, route.ended_at)
        self.assertEqual(len(parsed[0].raw_points), 3)
        self.assertGreater(route.distance_meters, 0)

    def test_route_without_timestamps_has_no_started_ended_at(self):
        parsed = gpx_tracks_to_routes(_gpx_bytes(_ROUTE_NO_TIMESTAMPS), self.profile, "loop.gpx")

        self.assertEqual(len(parsed), 1)
        route = parsed[0].route
        self.assertEqual(route.source, RouteSource.GPX_ROUTE)
        self.assertIsNone(route.started_at)
        self.assertIsNone(route.ended_at)

    def test_empty_track_is_skipped(self):
        body = "<trk><name>Empty</name><trkseg></trkseg></trk>"
        parsed = gpx_tracks_to_routes(_gpx_bytes(body), self.profile, "empty.gpx")
        self.assertEqual(parsed, [])

    def test_track_with_single_point_is_skipped(self):
        body = '<trk><name>One point</name><trkseg><trkpt lat="1.0" lon="1.0"/></trkseg></trk>'
        parsed = gpx_tracks_to_routes(_gpx_bytes(body), self.profile, "one.gpx")
        self.assertEqual(parsed, [])

    def test_real_world_sample_file(self):
        sample = (_SAMPLE_DATA_DIR / "sample.gpx").read_bytes()

        parsed = gpx_tracks_to_routes(sample, self.profile, "sample.gpx")

        # sample.gpx has 8 <trk> elements but the first ("ACTIVE LOG") is
        # empty (0 points) - only the 7 non-empty tracks should produce
        # Route candidates, totalling all 296 recorded trackpoints.
        self.assertEqual(len(parsed), 7)
        self.assertEqual(sum(p.route.raw_point_count for p in parsed), 296)
        for parsed_route in parsed:
            self.assertEqual(parsed_route.route.source, RouteSource.GPX_TRACK)
            self.assertGreaterEqual(parsed_route.route.simplified_point_count, 2)
            self.assertEqual(parsed_route.route.profile, self.profile)


class DetectDwellsAndCreateVisitsTests(TestCase):
    """detect_dwells_and_create_visits() creates PinVisit(source=GEOLOCATION) for qualifying dwells."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=str(_PIN_LAT), longitude=str(_PIN_LNG))
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
            latitude=None,
            longitude=None,
            point=Point(_PIN_LNG, _PIN_LAT, srid=4326),
        )

    def _saved_route(self, raw_points: list[RawTrackPoint]) -> Route:
        geometry = simplify_and_measure([(p.latitude, p.longitude) for p in raw_points])
        route = Route(
            profile=self.profile,
            source=RouteSource.GPX_TRACK,
            path=geometry.path,
            raw_point_count=geometry.raw_point_count,
            simplified_point_count=geometry.simplified_point_count,
            distance_meters=geometry.distance_meters,
        )
        route.save()
        return route

    def test_creates_geolocation_visit_for_qualifying_dwell(self):
        base = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        raw_points = [
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, base),
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, base + datetime.timedelta(minutes=5)),
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, base + datetime.timedelta(minutes=12)),
        ]
        route = self._saved_route(raw_points)

        created = detect_dwells_and_create_visits(route, raw_points, self.profile)

        self.assertEqual(created, 1)
        visit = PinVisit.objects.get(pin=self.pin, source=VisitSource.GEOLOCATION)
        self.assertEqual(visit.visited_at, base)
        self.assertEqual(visit.route_id, route.pk)

    def test_no_visit_for_dwell_shorter_than_minimum(self):
        base = timezone.make_aware(datetime.datetime(2024, 6, 1, 12, 0, 0))
        raw_points = [
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, base),
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, base + datetime.timedelta(minutes=3)),
        ]
        route = self._saved_route(raw_points)

        created = detect_dwells_and_create_visits(route, raw_points, self.profile)

        self.assertEqual(created, 0)
        self.assertFalse(PinVisit.objects.filter(pin=self.pin, source=VisitSource.GEOLOCATION).exists())

    def test_no_visit_without_any_timestamps(self):
        raw_points = [
            RawTrackPoint(_PIN_LAT + 0.0001, _PIN_LNG + 0.0001, None),
            RawTrackPoint(_PIN_LAT + 0.0002, _PIN_LNG + 0.0002, None),
        ]
        route = self._saved_route(raw_points)

        created = detect_dwells_and_create_visits(route, raw_points, self.profile)

        self.assertEqual(created, 0)
