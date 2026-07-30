"""Tests for RedataPlacesGateway against REData's shipped contract
(``../REData/docs/api-reference.md``, "Google Places API (New) - real place details").

Constructs the gateway with a mock ``session`` (Gateway.__post_init__ leaves a
non-default session untouched, skipping the DB-backed rate-limiting wrapper -
see gateway.py) so these stay pure unit tests with no database access.
"""

from __future__ import annotations

from unittest import mock

import pytest

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
from urbanlens.dashboard.services.gateway import GatewayRequestError


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.content = b"fake-bytes"
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataPlacesGateway:
    return RedataPlacesGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


class GetPlaceTests(SimpleTestCase):
    def test_200_returns_the_place_dict(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"place_id": "p1", "name": "Sydney Opera House"})

        result = _gateway(session).get_place("p1")

        self.assertEqual(result, {"place_id": "p1", "name": "Sydney Opera House"})
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/places/p1/")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_404_returns_none(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(404, {"error": "not_found"})

        self.assertIsNone(_gateway(session).get_place("missing"))

    def test_503_raises_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "rate_limited"})

        with pytest.raises(GatewayRequestError):
            _gateway(session).get_place("p1")


class SearchNearbyTests(SimpleTestCase):
    def test_parses_the_bare_array_response(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [{"place_id": "p1", "name": "Sydney Opera House"}])

        result = _gateway(session).search_nearby(1.0, 2.0, radius_meters=500, included_types=["historical_landmark"], max_results=10)

        self.assertEqual(result, [{"place_id": "p1", "name": "Sydney Opera House"}])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["latitude"], 1.0)
        self.assertEqual(params["longitude"], 2.0)
        self.assertEqual(params["radius_meters"], 500)
        self.assertEqual(params["max_results"], 10)
        self.assertEqual(params["included_type"], ["historical_landmark"])

    def test_omits_included_type_when_not_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).search_nearby(1.0, 2.0)

        self.assertNotIn("included_type", session.get.call_args.kwargs["params"])

    def test_non_200_raises_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(400, {"error": "invalid_request"})

        with pytest.raises(GatewayRequestError):
            _gateway(session).search_nearby(1.0, 2.0)

    def test_non_list_body_returns_empty_list(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"unexpected": "shape"})

        self.assertEqual(_gateway(session).search_nearby(1.0, 2.0), [])


class SearchTextTests(SimpleTestCase):
    def test_sends_query_and_optional_coordinates(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [{"place_id": "p1"}])

        result = _gateway(session).search_text("coffee near Main St", latitude=1.0, longitude=2.0)

        self.assertEqual(result, [{"place_id": "p1"}])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["query"], "coffee near Main St")
        self.assertEqual(params["latitude"], 1.0)
        self.assertEqual(params["longitude"], 2.0)

    def test_coordinates_omitted_when_not_given(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [])

        _gateway(session).search_text("coffee")

        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("latitude", params)
        self.assertNotIn("longitude", params)


class AutocompleteTests(SimpleTestCase):
    def test_parses_the_bare_array_response(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, [{"kind": "place", "place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}])

        result = _gateway(session).autocomplete("sydney opera")

        self.assertEqual(result[0]["main_text"], "Sydney Opera House")

    def test_non_200_raises_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(400, {"error": "invalid_request"})

        with pytest.raises(GatewayRequestError):
            _gateway(session).autocomplete("")


class DownloadPhotoTests(SimpleTestCase):
    def test_200_returns_content_and_content_type(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, None)

        result = _gateway(session).download_photo("p1", 5)

        self.assertEqual(result, (b"fake-bytes", "image/jpeg"))
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/places/p1/photos/5/download/")

    def test_404_returns_none(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(404, {"error": "photo_unavailable"})

        self.assertIsNone(_gateway(session).download_photo("p1", 5))

    def test_503_raises_gateway_request_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(503, {"error": "places_api_unavailable"})

        with pytest.raises(GatewayRequestError):
            _gateway(session).download_photo("p1", 5)


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            RedataPlacesGateway(base_url=None, api_key="test-key", session=mock.Mock())

    def test_missing_api_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            RedataPlacesGateway(base_url="https://redata.example.test", api_key=None, session=mock.Mock())
