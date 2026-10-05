"""Google Open Buildings is read a level-4 S2 cell's shard at a time, and a real shard runs to gigabytes (P301).

Lagos's is 1.9 GB compressed, Sao Paulo's 2.4 GB, Dhaka's 3.4 GB. A boundary lookup downloaded the shard whole and
decompressed it in memory. In the US, where the dataset has no shards, dev asked for the same missing one 1,210 times.
"""

from __future__ import annotations

import gzip
import io
from unittest import mock
from unittest.mock import MagicMock

from django.core.cache import cache
from urllib3 import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.boundaries import google_open_buildings
from urbanlens.dashboard.services.apis.locations.boundaries.google_open_buildings import GoogleOpenBuildingsGateway

_BBOX = (-73.94, 41.72, -73.92, 41.74)
_CSV = (
    "latitude,longitude,area_in_meters,confidence,geometry,full_plus_code\n"
    '41.73,-73.93,120.5,0.9,"POLYGON ((-73.9301 41.7299, -73.9299 41.7299, -73.9299 41.7301, -73.9301 41.7299))",87H\n'
)


def _response(status: int, body: bytes = b"", *, length: int | None = None, headers: bool = True) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Length": str(len(body) if length is None else length)} if headers else {}
    response._content_consumed = False
    response.raw = HTTPResponse(body=io.BytesIO(body), status=status, preload_content=False)
    response.raise_for_status = MagicMock()
    return response


class ShardTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.session = MagicMock()
        self.gateway = GoogleOpenBuildingsGateway(session=self.session)

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
        self.assertEqual(self.session.get.call_count, len(google_open_buildings._s2_tokens_for_bbox(_BBOX)))

    def test_a_shard_that_does_not_say_its_size_is_read_no_further_than_the_cap(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(_CSV.encode()), headers=False)

        with mock.patch.object(google_open_buildings, "MAX_SHARD_BYTES", 16):
            self.assertEqual(self.gateway.get_building_points(_BBOX), [])
            asked = self.session.get.call_count
            self.assertEqual(self.gateway.get_building_points(_BBOX), [])

        self.assertEqual(self.session.get.call_count, asked)

    def test_a_small_shard_is_read(self) -> None:
        self.session.get.return_value = _response(200, gzip.compress(_CSV.encode()))

        points = self.gateway.get_building_points(_BBOX)

        self.assertEqual([(point["latitude"], point["longitude"]) for point in points], [(41.73, -73.93)])
        self.assertEqual(points[0]["area_in_meters"], 120.5)
