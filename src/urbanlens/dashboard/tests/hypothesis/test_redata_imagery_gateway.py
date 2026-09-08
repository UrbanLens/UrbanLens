"""Tests for RedataImageryGateway - REData's ``/imagery/`` cross-provider imagery endpoint.

Mirrors ``test_redata_context_gateway.py``'s conventions: a mock ``session``
(``Gateway.__post_init__`` leaves a non-default session untouched, skipping
the DB-backed rate-limiting wrapper), no database access.
"""

from __future__ import annotations

import datetime
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
        results = [
            {
                "provider": "nasa_gibs",
                "url": "https://gibs.example/tile.jpg",
                "delivery": "image",
                "captured_on": "2019",
                "attribution": "NASA",
            }
        ]
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


class DownloadArchivedCopyTests(SimpleTestCase):
    def test_hits_the_download_path_for_the_given_uuid(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"\xff\xd8\xff")

        result = _gateway(session).download_archived_copy("abc-123")

        self.assertEqual(result, b"\xff\xd8\xff")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/imagery/abc-123/download/")

    def test_sends_width_and_height_as_query_params(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"bytes")

        _gateway(session).download_archived_copy("abc-123", width=1024, height=768)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params, {"width": 1024, "height": 768})

    def test_omits_width_and_height_when_not_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"bytes")

        _gateway(session).download_archived_copy("abc-123")

        self.assertEqual(session.get.call_args.kwargs["params"], {})

    def test_sends_the_bearer_auth_header(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, content=b"bytes")

        _gateway(session).download_archived_copy("abc-123")

        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-key")

    def test_non_200_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(404, {"error": "no_imagery", "message": "nothing here"})

        with pytest.raises(LocationContextUnavailableError):
            _gateway(session).download_archived_copy("abc-123")

    def test_date_required_for_an_unmaterialized_time_series_row(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(400, {"error": "date_required", "message": "POST /imagery/capture/ first"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).download_archived_copy("abc-123")
        self.assertEqual(ctx.value.reason, "date_required")


class CaptureTimeSeriesTests(SimpleTestCase):
    def test_posts_asset_uuid_date_and_size(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"uuid": "child-uuid", "provider": "nasa_gibs", "delivery": "wms"})

        _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15), width=512, height=384)

        url = session.post.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/imagery/capture/")
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body, {"asset_uuid": "parent-uuid", "date": "2005-06-15", "width": 512, "height": 384})

    def test_defaults_width_and_height_to_1024(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"uuid": "child-uuid"})

        _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))

        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["width"], 1024)
        self.assertEqual(body["height"], 1024)

    def test_200_returns_the_materialized_row(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"uuid": "child-uuid", "captured_on": "2005-06-15"})

        result = _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))

        self.assertEqual(result, {"uuid": "child-uuid", "captured_on": "2005-06-15"})

    def test_400_date_unavailable_returns_none(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(400, {"error": "date_unavailable", "message": "outside coverage"})

        result = _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))

        self.assertIsNone(result)

    def test_404_no_imagery_returns_none(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(404, {"error": "no_imagery", "message": "nothing that day"})

        result = _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))

        self.assertIsNone(result)

    def test_503_rate_limited_raises_with_reason(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(503, {"error": "rate_limited", "message": "back off"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))
        self.assertEqual(ctx.value.reason, "rate_limited")

    def test_503_imagery_unavailable_raises_with_reason(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(503, {"error": "imagery_unavailable", "message": "source down"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))
        self.assertEqual(ctx.value.reason, "imagery_unavailable")

    def test_a_400_for_an_undocumented_reason_still_raises(self) -> None:
        """Only the two documented "nothing here" reasons are swallowed."""
        session = mock.Mock()
        session.post.return_value = _response(400, {"error": "invalid_parameter", "message": "bad asset_uuid"})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))
        self.assertEqual(ctx.value.reason, "invalid_parameter")

    def test_unexpected_status_code_raises_source_error(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(500, {})

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))
        self.assertEqual(ctx.value.reason, "source_error")

    def test_network_error_raises_source_error(self) -> None:
        session = mock.Mock()
        session.post.side_effect = OSError("connection refused")

        with pytest.raises(LocationContextUnavailableError) as ctx:
            _gateway(session).capture_time_series("parent-uuid", datetime.date(2005, 6, 15))
        self.assertEqual(ctx.value.reason, "source_error")
