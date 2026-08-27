"""Tests for RedataImageryGateway - REData's ``/imagery/`` cross-provider imagery endpoint.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mock ``session``
(``Gateway.__post_init__`` leaves a non-default session untouched, skipping
the DB-backed rate-limiting wrapper), no database access.
"""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_imagery_gateway import RedataImageryGateway


def _response(status_code: int, body: object = None, content: bytes = b"") -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.content = content
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataImageryGateway:
    return RedataImageryGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetImageryTests(SimpleTestCase):
    def test_hits_the_imagery_path(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).get_imagery(41.7, -73.9)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/imagery/")

    def test_returns_the_envelopes_results(self) -> None:
        session = mock.Mock()
        results = [{"provider": "nasa_gibs", "url": "https://gibs.example/tile.jpg", "delivery": "image", "captured_on": "2019", "attribution": "NASA"}]
        session.get.return_value = _response(200, {"count": 1, "complete": True, "results": results})

        result = _gateway(session).get_imagery(41.7, -73.9)

        self.assertEqual(result, results)

    def test_forwards_a_provider_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": []})

        _gateway(session).get_imagery(41.7, -73.9, providers=["nasa_gibs", "mapbox"])

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["provider"], ["nasa_gibs", "mapbox"])

    def test_every_provider_failing_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "all_providers_unavailable", "message": "no dice"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).get_imagery(41.7, -73.9)
        self.assertEqual(ctx.value.reason, "all_providers_unavailable")


class DownloadBytesTests(SimpleTestCase):
    def test_fetches_a_relative_download_path_off_base_url(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"\xff\xd8\xff")

        result = _gateway(session).download_bytes("/api/v1/imagery/abc-123/download/")

        self.assertEqual(result, b"\xff\xd8\xff")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/imagery/abc-123/download/")

    def test_fetches_an_absolute_url_as_is(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"\xff\xd8\xff")

        _gateway(session).download_bytes("https://other.example.test/imagery/abc-123/download/")

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://other.example.test/imagery/abc-123/download/")

    def test_sends_the_bearer_auth_header(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"bytes")

        _gateway(session).download_bytes("/download/")

        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-key")

    def test_non_200_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "source_error", "message": "boom"})

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).download_bytes("/download/")
