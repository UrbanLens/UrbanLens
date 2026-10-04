"""A route or a visit an import builds is on the globe, whatever coordinates the file held (P284).

P282 checked every coordinate an import turns into a pin. The same files also make routes, whose ``path`` is stored,
and visits, matched to the nearest pin; an infinite, NaN or out-of-range point reached both.
"""

from __future__ import annotations

import json
import math

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.services.apis.locations.google.location_history import (
    semantic_history_to_routes,
    semantic_visit,
)
from urbanlens.dashboard.services.import_export.import_data import ImportContext, ImportResult, RoutesImport
from urbanlens.dashboard.services.import_formats.gpx_tracks import gpx_tracks_to_routes

#: A ``latE7``/``lngE7`` value no place on the globe has: past the range, past a float, not finite, not a number.
_OFF_THE_GLOBE_E7 = ("950000000", "Infinity", "NaN", "1" + "0" * 400, '"404000000"', "true")


def _gpx(*points: tuple[str, str]) -> bytes:
    trkpts = "".join(
        f'<trkpt lat="{lat}" lon="{lng}"><time>2024-01-01T10:0{index}:00Z</time></trkpt>'
        for index, (lat, lng) in enumerate(points)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>Walk</name><trkseg>{trkpts}</trkseg></trk></gpx>"
    ).encode()


def _on_the_globe(longitude: float, latitude: float) -> bool:
    return math.isfinite(latitude + longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180


class _Profiled(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make(User).profile


class GpxTrackTests(_Profiled):
    def test_a_point_off_the_globe_is_left_out_of_the_route(self) -> None:
        for latitude in ("9999", "inf", "-inf", "nan", "1e999"):
            with self.subTest(latitude=latitude):
                (parsed,) = gpx_tracks_to_routes(
                    _gpx(("40.000", "-73.000"), (latitude, "-73.001"), ("40.002", "-73.002")), self.profile, "w.gpx"
                )

                self.assertEqual(len(parsed.raw_points), 2)
                self.assertTrue(all(_on_the_globe(*coordinate) for coordinate in parsed.route.path.coords))

    def test_a_track_left_with_one_point_is_no_route(self) -> None:
        self.assertEqual(gpx_tracks_to_routes(_gpx(("40.0", "-73.0"), ("95", "-73.0")), self.profile, "w.gpx"), [])


class SemanticHistoryTests(_Profiled):
    def _segment(self, lat_e7: str) -> dict:
        points = ", ".join(
            f'{{"latE7": {lat}, "lngE7": -730000000, "timestamp": "2024-01-01T10:0{index}:00Z"}}'
            for index, lat in enumerate(("400000000", lat_e7, "400020000"))
        )
        return json.loads(
            f'{{"timelineObjects": [{{"activitySegment": {{"simplifiedRawPath": {{"points": [{points}]}}}}}}]}}'
        )

    def test_a_route_point_off_the_globe_is_left_out(self) -> None:
        for lat_e7 in _OFF_THE_GLOBE_E7:
            with self.subTest(lat_e7=lat_e7[:12]):
                (parsed,) = semantic_history_to_routes(self._segment(lat_e7), self.profile, "2019_JANUARY.json")

                self.assertEqual(len(parsed.raw_points), 2)
                self.assertTrue(all(_on_the_globe(*coordinate) for coordinate in parsed.route.path.coords))

    def test_a_visit_off_the_globe_is_no_visit(self) -> None:
        for lat_e7 in _OFF_THE_GLOBE_E7:
            with self.subTest(lat_e7=lat_e7[:12]):
                obj = json.loads(
                    f'{{"placeVisit": {{"location": {{"latitudeE7": {lat_e7}, "longitudeE7": -730000000}}, '
                    '"duration": {"startTimestamp": "2024-01-01T10:00:00Z"}}}'
                )

                self.assertIsNone(semantic_visit(obj))

    def test_a_visit_on_the_globe_is_read(self) -> None:
        """Anti-vacuity for the test above."""
        obj = {
            "placeVisit": {
                "location": {"latitudeE7": 404000000, "longitudeE7": -730000000},
                "duration": {"startTimestamp": "2024-01-01T10:00:00Z"},
            }
        }

        self.assertEqual((semantic_visit(obj) or {}).get("latitude"), 40.4)


class ExportedRouteTests(_Profiled):
    def _import(self, row: dict) -> bool:
        context = ImportContext(
            profile=self.profile, data_dir="", result=ImportResult(), pin_uuid_map={}, label_uuid_map={}
        )
        return RoutesImport().import_row(row, context)

    def test_a_path_off_the_globe_is_not_restored(self) -> None:
        for coordinate in ([500.0, 10.0], [10.0, -91.0], [math.inf, 0.0], [math.nan, 0.0]):
            with self.subTest(coordinate=coordinate):
                row = {"path": {"type": "LineString", "coordinates": [[-73.0, 40.0], coordinate]}}

                self.assertFalse(self._import(row))
                self.assertFalse(Route.objects.exists())

    def test_a_measure_that_is_not_finite_is_not_restored(self) -> None:
        row = {
            "path": {"type": "LineString", "coordinates": [[-73.0, 40.0], [-73.1, 40.1]]},
            "distance_meters": math.inf,
            "elevation_gain_meters": math.nan,
        }

        self.assertTrue(self._import(row))
        route = Route.objects.get()
        self.assertEqual((route.distance_meters, route.elevation_gain_meters), (0.0, None))
