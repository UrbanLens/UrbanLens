"""Google Open Buildings is read one S2 cell's shard at a time, and a real shard runs to gigabytes (P301, P319).

Its level-4 shards are Lagos's 1.9 GB compressed, Sao Paulo's 2.4 GB, Dhaka's 3.4 GB, and 200 of the 333 run past the
cap a lookup will read. The same v3 release is published as 3,330 level-6 shards with no header row, a median of
8.8 MiB, of which 2,704 fit. The dataset covers 333 level-4 cells, none of them in the US, where dev asked for the same
missing shard 1,210 times.
"""

from __future__ import annotations

import gzip
import io
from unittest import mock
from unittest.mock import MagicMock

from django.core.cache import cache
import requests
import s2sphere
from urllib3 import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.boundaries import google_open_buildings
from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway

#: The Royal Palace in Luang Prabang, Laos, at its Wikipedia coordinate; inside level-6 cell 312f.
_PALACE = (19.8921, 102.1356)
_BBOX = (102.1346, 19.8911, 102.1366, 19.8931)
#: Poughkeepsie, NY: outside the dataset.
_US_BBOX = (-73.94, 41.72, -73.92, 41.74)
#: The palace's own row from ``v3/polygons_s2_level_6_gzip_no_header/312f_buildings.csv.gz``, fetched 2026-10-05.
_PALACE_ROW = (
    "19.89225351,102.13550544,2382.1336,0.8942,"
    '"POLYGON((102.135598968091 19.891987405097, 102.135748973252 19.8920969683974, 102.135725489704 19.8921257378274, '
    "102.135829199297 19.8922014868503, 102.135807022162 19.8922286558121, 102.135835285695 19.8922492993517, "
    "102.135740792499 19.8923650617911, 102.13563975405 19.8922912637377, 102.135590138205 19.8923520473559, "
    "102.135617048915 19.8923717028417, 102.135468341218 19.8925538820128, 102.135410495791 19.892511631869, "
    "102.13536939463 19.8925619840496, 102.135219389382 19.8924524204131, 102.135260490553 19.8924020682687, "
    "102.135210289836 19.8923654016829, 102.135358997593 19.8921832227397, 102.135389299442 19.8922053551813, "
    "102.135438915303 19.892144571622, 102.135329346013 19.8920645422935, 102.135423839271 19.8919487800834, "
    "102.135452182393 19.8919694818798, 102.135474359539 19.8919423129659, 102.135575484538 19.8920161745062, "
    '102.135598968091 19.891987405097))",7PF4V4RP+W622\n'
)
#: A row outside ``_BBOX``, which the same shard also holds.
_FAR_ROW = '19.87547640,102.12248919,125.4018,0.8511,"POLYGON((102.1224 19.8754, 102.1225 19.8754, 102.1225 19.8755, 102.1224 19.8754))",7PF4V4GC+5XX4\n'
_POINTS = (
    "19.89225351,102.13550544,2382.1336,0.8942,7PF4V4RP+W622\n19.87547640,102.12248919,125.4018,0.8511,7PF4V4GC+5XX4\n"
)


def _response(status: int, body: bytes = b"", *, length: int | None = None, headers: bool = True) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Length": str(len(body) if length is None else length)} if headers else {}
    response._content_consumed = False
    response.raw = HTTPResponse(body=io.BytesIO(body), status=status, preload_content=False)
    response.raise_for_status = MagicMock()
    return response


class _GatewayTestCase(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.session = MagicMock()
        self.gateway = GoogleOpenBuildingsGateway(session=self.session)

    def requested(self) -> list[str]:
        return [call.args[0] for call in self.session.get.call_args_list]


class ShardTests(_GatewayTestCase):
    def test_a_missing_shard_is_asked_for_once(self) -> None:
        self.session.get.return_value = _response(404)

        self.assertEqual(self.gateway.get_building_points(_BBOX), [])
        asked = self.session.get.call_count
        self.assertEqual(self.gateway.get_building_points(_BBOX), [])

        self.assertGreater(asked, 0)
        self.assertEqual(self.session.get.call_count, asked)

    def test_a_shard_past_the_cap_is_not_read(self) -> None:
        huge = _response(200, b"", length=google_open_buildings.MAX_SHARD_BYTES + 1)
        huge.raw = MagicMock()
        self.session.get.return_value = huge

        self.assertEqual(self.gateway.get_building_points(_BBOX), [])
        self.assertEqual(self.gateway.get_building_points(_BBOX), [])

        huge.raw.read.assert_not_called()
        self.assertTrue(all(call.kwargs.get("stream") for call in self.session.get.call_args_list))
        self.assertEqual(self.session.get.call_count, len(google_open_buildings._shard_tokens_for_bbox(_BBOX)))

    def test_a_shard_that_does_not_say_its_size_is_read_no_further_than_the_cap(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(_POINTS.encode()), headers=False)

        with mock.patch.object(google_open_buildings, "MAX_SHARD_BYTES", 16):
            self.assertEqual(self.gateway.get_building_points(_BBOX), [])
            asked = self.session.get.call_count
            self.assertEqual(self.gateway.get_building_points(_BBOX), [])

        self.assertEqual(self.session.get.call_count, asked)

    def test_a_small_shard_is_read(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(_POINTS.encode()))

        points = self.gateway.get_building_points(_BBOX)

        self.assertEqual([(point["latitude"], point["longitude"]) for point in points], [(19.89225351, 102.13550544)])
        self.assertEqual(points[0]["area_in_meters"], 2382.1336)
        self.assertEqual(points[0]["full_plus_code"], "7PF4V4RP+W622")


class CoverageTests(_GatewayTestCase):
    """Only cells the dataset covers are asked for, at the level whose shards a lookup can read."""

    def test_a_place_outside_the_dataset_asks_for_nothing(self) -> None:
        self.assertEqual(self.gateway.get_buildings(_US_BBOX), [])
        self.assertEqual(self.gateway.get_building_points(_US_BBOX), [])
        self.assertIsNone(self.gateway.get_boundary(41.73, -73.93))

        self.session.get.assert_not_called()

    def test_a_covered_place_asks_for_its_level_6_shard(self) -> None:
        self.session.get.return_value = _response(404)

        self.gateway.get_buildings(_BBOX)
        self.gateway.get_building_points(_BBOX)

        self.assertEqual(
            self.requested(),
            [
                "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_6_gzip_no_header/312f_buildings.csv.gz",
                "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_6_gzip_no_header/312f_buildings.csv.gz",
            ],
        )

    def test_the_covered_cells_are_the_333_level_4_cells_v3_publishes(self) -> None:
        cells = google_open_buildings.covered_level_4_cells()

        self.assertEqual(len(cells), 333)
        self.assertEqual({s2sphere.CellId.from_token(token).level() for token in cells}, {4})
        self.assertIn("313", cells)
        self.assertNotIn("89d", cells)


class HeaderlessShardTests(_GatewayTestCase):
    """A level-6 shard has no header row; its columns are the level-4 shards' header, in order."""

    def test_polygons_are_read_without_a_header(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress((_PALACE_ROW + _FAR_ROW).encode()))

        buildings = self.gateway.get_buildings(_BBOX)

        self.assertEqual(len(buildings), 1)
        self.assertEqual(buildings[0]["geometry"]["type"], "Polygon")
        self.assertEqual(
            buildings[0]["properties"],
            {"area_in_meters": 2382.1336, "confidence": 0.8942, "full_plus_code": "7PF4V4RP+W622"},
        )

    def test_the_raw_rows_keep_their_wkt(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(_PALACE_ROW.encode()))

        (building,) = self.gateway.get_buildings(_BBOX, as_geojson=False)

        self.assertTrue(building["geometry_wkt"].startswith("POLYGON((102.135598968091 19.891987405097"))
        self.assertEqual(building["confidence"], 0.8942)

    def test_the_boundary_is_the_footprint_holding_the_point(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress((_PALACE_ROW + _FAR_ROW).encode()))

        boundary = self.gateway.get_boundary(*_PALACE)

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertTrue(boundary.contains(boundary.centroid))
        self.assertAlmostEqual(boundary.centroid.x, 102.13552, places=4)
        self.assertAlmostEqual(boundary.centroid.y, 19.89224, places=4)

    def test_a_short_row_is_skipped_rather_than_failing_the_shard(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(("19.8922,102.1355\n" + _POINTS).encode()))

        points = self.gateway.get_building_points(_BBOX)

        self.assertEqual([point["full_plus_code"] for point in points], ["7PF4V4RP+W622"])


class RateLimitedSessionTests(TestCase):
    """The gateway's own session, which logs each call, passes ``stream`` through and leaves the body unread."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_shard_past_the_cap_is_not_read_through_the_real_session(self) -> None:
        gateway = GoogleOpenBuildingsGateway()
        huge = _response(200, b"", length=google_open_buildings.MAX_SHARD_BYTES + 1)
        huge.raw = MagicMock()
        huge.ok = True

        with mock.patch.object(requests.Session, "request", return_value=huge) as request:
            self.assertEqual(gateway.get_building_points(_BBOX), [])

        self.assertTrue(request.call_args.kwargs["stream"])
        huge.raw.read.assert_not_called()

    def test_a_place_outside_the_dataset_logs_no_call(self) -> None:
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog

        with mock.patch.object(requests.Session, "request") as request:
            self.assertIsNone(GoogleOpenBuildingsGateway().get_boundary(41.73, -73.93))

        request.assert_not_called()
        self.assertFalse(ApiCallLog.objects.filter(service="google_open_buildings").exists())
