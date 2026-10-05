"""Microsoft's building footprints are read a zoom-9 quadkey's part at a time, and a part runs to 292 MB (P309).

The links file lists 178 parts over 64 MiB, the largest Tokyo's at 292 MB compressed and one in the US at 170 MB. A
boundary lookup downloaded the part whole, decompressed it whole and split the text into a list of lines, so its
decompressed size was held twice. On dev, the Hudson Valley's 16.7 MB part was downloaded 208 times in a week.
"""

from __future__ import annotations

import gzip
import io
import json
import tracemalloc
from unittest import mock
from unittest.mock import MagicMock

from django.core.cache import cache
import requests
from urllib3 import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.boundaries import microsoft_buildings
from urbanlens.dashboard.services.apis.locations.boundaries.microsoft_buildings import (
    MicrosoftBuildingFootprintsGateway,
    quadkeys_for_bbox,
)

_BBOX = (-73.931, 41.729, -73.929, 41.731)
_URL = "https://minedbuildings.example/quadkey={quadkey}/part-00035.csv.gz"


def _square(lon: float, lat: float, half: float = 0.0001) -> dict:
    ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half], [lon - half, lat + half]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[*ring, ring[0]]]}, "properties": {}}


_INSIDE = _square(-73.93, 41.73)
_OUTSIDE = _square(-73.5, 41.5)


def _part(*features: dict) -> bytes:
    return gzip.compress("".join(json.dumps(feature) + "\n" for feature in features).encode())


def _response(status: int, body: bytes = b"", *, length: int | None = None, headers: bool = True) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Length": str(len(body) if length is None else length)} if headers else {}
    response._content_consumed = False
    response.raw = HTTPResponse(body=io.BytesIO(body), status=status, preload_content=False)
    response.content = body
    response.raise_for_status = MagicMock()
    return response


def _links(size: str = "16.0MB") -> list[dict[str, str]]:
    (quadkey,) = quadkeys_for_bbox(_BBOX, zoom=9)
    return [{"Location": "UnitedStates", "QuadKey": quadkey, "Url": _URL.format(quadkey=quadkey), "Size": size}]


class PartTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.session = MagicMock()

    def _gateway(self, size: str = "16.0MB") -> MicrosoftBuildingFootprintsGateway:
        return MicrosoftBuildingFootprintsGateway(session=self.session, _dataset_links=_links(size))

    def test_a_small_part_is_read(self) -> None:
        self.session.get.return_value = _response(200, _part(_OUTSIDE, _INSIDE))

        features = self._gateway().get_buildings(_BBOX)

        self.assertEqual(features, [_INSIDE])

    def test_a_part_the_links_file_lists_past_the_cap_is_not_asked_for(self) -> None:
        self.assertEqual(self._gateway("170.3MB").get_buildings(_BBOX), [])

        self.session.get.assert_not_called()

    def test_a_part_past_the_cap_by_its_headers_is_not_read(self) -> None:
        huge = _response(200, b"", length=microsoft_buildings.MAX_PART_BYTES + 1)
        huge.raw = MagicMock()
        self.session.get.return_value = huge

        self.assertEqual(self._gateway().get_buildings(_BBOX), [])
        self.assertEqual(self._gateway().get_buildings(_BBOX), [])

        huge.raw.read.assert_not_called()
        self.assertTrue(all(call.kwargs.get("stream") for call in self.session.get.call_args_list))
        self.assertEqual(self.session.get.call_count, 1)

    def test_a_part_that_does_not_say_its_size_is_read_no_further_than_the_cap(self) -> None:
        self.session.get.return_value = _response(200, _part(_INSIDE), headers=False)

        with mock.patch.object(microsoft_buildings, "MAX_PART_BYTES", 16):
            self.assertEqual(self._gateway(size="").get_buildings(_BBOX), [])
            self.assertEqual(self._gateway(size="").get_buildings(_BBOX), [])

        self.assertEqual(self.session.get.call_count, 1)

    def test_a_missing_part_is_asked_for_once(self) -> None:
        self.session.get.return_value = _response(404)

        self.assertEqual(self._gateway().get_buildings(_BBOX), [])
        self.assertEqual(self._gateway().get_buildings(_BBOX), [])

        self.assertEqual(self.session.get.call_count, 1)

    def test_a_part_is_decompressed_a_line_at_a_time(self) -> None:
        """Only the buildings within the box are kept; the part's text is never held whole."""
        far = [_square(-73.5 + n * 1e-6, 41.5) for n in range(40_000)]
        body = _part(*far, _INSIDE)
        text_size = len(gzip.decompress(body))
        self.session.get.return_value = _response(200, body)

        tracemalloc.start()
        try:
            features = self._gateway().get_buildings(_BBOX)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(features, [_INSIDE])
        self.assertLess(peak, text_size / 2)

    def test_a_building_far_from_the_box_is_not_parsed(self) -> None:
        """Parsing each of a part's 170,824 buildings took 17 s on dev to keep the few near the point."""
        far = [_square(-73.5 + n * 1e-5, 41.5) for n in range(500)]
        self.session.get.return_value = _response(200, _part(*far, _INSIDE))

        with mock.patch.object(microsoft_buildings.json, "loads", wraps=json.loads) as parsed:
            features = self._gateway().get_buildings(_BBOX)

        self.assertEqual(features, [_INSIDE])
        self.assertLess(parsed.call_count, 10)

    def test_a_building_reaching_into_the_box_from_outside_is_kept(self) -> None:
        """Its first vertex lies outside the box; the building still overlaps it."""
        straddling = _square(-73.9312, 41.7300, half=0.0005)
        multi = {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [_square(-73.9305, 41.7305)["geometry"]["coordinates"]],
            },
            "properties": {"height": -1.0},
        }
        self.session.get.return_value = _response(200, _part(straddling, multi))

        self.assertEqual(self._gateway().get_buildings(_BBOX), [straddling, multi])

    def test_a_multipart_building_whose_first_part_is_far_is_kept(self) -> None:
        """Its first vertex lies far from the box; a later part lies inside it."""
        parts = [_OUTSIDE["geometry"]["coordinates"], _INSIDE["geometry"]["coordinates"]]
        multi = {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": parts}, "properties": {}}
        self.session.get.return_value = _response(200, _part(multi))

        self.assertEqual(self._gateway().get_buildings(_BBOX), [multi])

    def test_a_server_error_is_not_remembered_as_unusable(self) -> None:
        failing = _response(503)
        failing.raise_for_status.side_effect = requests.HTTPError("503")
        self.session.get.side_effect = [failing, _response(200, _part(_INSIDE))]

        with self.assertRaises(requests.HTTPError):
            self._gateway().get_buildings(_BBOX)

        self.assertEqual(self._gateway().get_buildings(_BBOX), [_INSIDE])
        self.assertEqual(self.session.get.call_count, 2)


class RateLimitedSessionTests(TestCase):
    """The gateway's own session, which logs each call, passes ``stream`` through and leaves the body unread."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_part_past_the_cap_is_not_read_through_the_real_session(self) -> None:
        gateway = MicrosoftBuildingFootprintsGateway(_dataset_links=_links())
        huge = _response(200, b"", length=microsoft_buildings.MAX_PART_BYTES + 1)
        huge.raw = MagicMock()
        huge.ok = True

        with mock.patch.object(requests.Session, "request", return_value=huge) as request:
            self.assertEqual(gateway.get_buildings(_BBOX), [])

        self.assertTrue(request.call_args.kwargs["stream"])
        huge.raw.read.assert_not_called()
