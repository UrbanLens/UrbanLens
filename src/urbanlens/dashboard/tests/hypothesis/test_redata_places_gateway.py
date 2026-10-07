"""Tests for RedataPlacesGateway against REData's shipped contract (``../REData/docs/api-reference.md``, "Google Places API (New) - real place details")."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
import requests
from urllib3 import HTTPResponse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError, GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker
from urbanlens.dashboard.tests.hypothesis.redata_helpers import BUDGET_REFUSALS

if TYPE_CHECKING:
    from collections.abc import Callable

_BUDGET_REFUSED = tuple(
    {"error": error, "message": "Places API (New) request budget is exhausted right now."} for error in BUDGET_REFUSALS
)


def _response(status_code: int, body: object, headers: dict[str, str] | None = None) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.headers = {"Content-Type": "image/jpeg", **(headers or {})}
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

        result = _gateway(session).search_nearby(
            1.0, 2.0, radius_meters=500, included_types=["historical_landmark"], max_results=10
        )

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
        session.get.return_value = _response(
            200,
            [{"kind": "place", "place_id": "p1", "main_text": "Sydney Opera House", "secondary_text": "Sydney NSW"}],
        )

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
        streamed = requests.Response()
        streamed.status_code = 200
        streamed.headers["Content-Type"] = "image/jpeg"
        streamed.raw = HTTPResponse(body=io.BytesIO(b"fake-bytes"), status=200, preload_content=False)
        session.get.return_value = streamed

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


def _every_call(gateway: RedataPlacesGateway) -> dict[str, Callable[[], Any]]:
    return {
        "get_place": lambda: gateway.get_place("p1"),
        "search_nearby": lambda: gateway.search_nearby(41.7, -73.9),
        "search_text": lambda: gateway.search_text("asylum"),
        "autocomplete": lambda: gateway.autocomplete("asy"),
        "download_photo": lambda: gateway.download_photo("p1", 5),
    }


class RateLimitedTests(SimpleTestCase):
    """REData's ``503 rate_limited`` is its Places budget spent, and ``503 key_budget_exhausted`` this key's share of it.

    The caller is told how long to leave either, and neither is an answer about the place.
    """

    def _raised(self, call: str, response: mock.Mock) -> GatewayRequestError:
        session = mock.Mock()
        session.get.return_value = response
        with pytest.raises(GatewayRequestError) as caught:
            _every_call(_gateway(session))[call]()
        return caught.value

    def test_the_wait_redata_named_is_passed_on(self) -> None:
        for body in _BUDGET_REFUSED:
            for call in _every_call(_gateway(mock.Mock())):
                with self.subTest(call, error=body["error"]):
                    raised = self._raised(call, _response(503, body, {"Retry-After": "120"}))

                    assert isinstance(raised, UpstreamBusyError)
                    self.assertEqual(raised.retry_after, 120)
                    self.assertIsInstance(raised, GatewayRateLimitedError, "an enrichment run still stops at it")

    def test_without_a_wait_it_holds_off_as_long_as_the_breaker_does(self) -> None:
        for body in _BUDGET_REFUSED:
            for call in _every_call(_gateway(mock.Mock())):
                with self.subTest(call, error=body["error"]):
                    raised = self._raised(call, _response(503, body))

                    self.assertIsInstance(raised, GatewayRateLimitedError)
                    assert isinstance(raised, UpstreamBusyError)
                    self.assertEqual(raised.retry_after, RedataBreaker.SOURCE_BUSY_SECONDS)
                    self.assertTrue(
                        raised.is_outage, "a spent budget says nothing about the place, so nothing is cached"
                    )

    def test_a_provider_throttle_redata_relays_is_a_rate_limit_too(self) -> None:
        """Google's own 429, passed on by REData as ``places_api_unavailable``, is the same shared budget saying no.

        Production (0.8.0) logged 18 of these in 13 h, each followed by the next location's call.
        """
        relayed = {
            "error": "places_api_unavailable",
            "message": "Places API (New) answered 429 for https://places.googleapis.com/v1/places:searchNearby.",
        }
        for headers, wait in (({}, RedataBreaker.SOURCE_BUSY_SECONDS), ({"Retry-After": "300"}, 300)):
            for call in _every_call(_gateway(mock.Mock())):
                with self.subTest(call, headers=headers):
                    raised = self._raised(call, _response(503, relayed, headers))

                    self.assertIsInstance(raised, GatewayRateLimitedError, "an enrichment run stops at it")
                    assert isinstance(raised, UpstreamBusyError)
                    self.assertEqual(raised.retry_after, wait)

    def test_any_answer_naming_a_wait_is_a_rate_limit(self) -> None:
        """REData's own throttle (429), and a 503 that says when to come back, both mean "not now"."""
        answers = (
            _response(
                429, {"detail": "Request was throttled. Expected available in 600 seconds."}, {"Retry-After": "600"}
            ),
            _response(
                503,
                {"error": "places_api_unavailable", "message": "Places API (New) answered 503."},
                {"Retry-After": "45"},
            ),
        )
        for response in answers:
            for call in _every_call(_gateway(mock.Mock())):
                with self.subTest(call, status=response.status_code):
                    raised = self._raised(call, response)

                    self.assertIsInstance(raised, GatewayRateLimitedError)
                    assert isinstance(raised, UpstreamBusyError)
                    self.assertEqual(raised.retry_after, int(response.headers["Retry-After"]))

    def test_other_failures_are_unchanged(self) -> None:
        failures = (
            _response(503, {"error": "places_api_unavailable", "message": "Places API (New) answered 500"}),
            _response(500, {"error": "server_error"}),
            _response(400, {"error": "invalid_parameter"}),
        )
        for response in failures:
            for call in _every_call(_gateway(mock.Mock())):
                with self.subTest(call, status=response.status_code):
                    self.assertIs(type(self._raised(call, response)), GatewayRequestError)


class ConstructionTests(SimpleTestCase):
    def test_missing_base_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            RedataPlacesGateway(base_url=None, api_key="test-key", session=mock.Mock())

    def test_missing_api_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            RedataPlacesGateway(base_url="https://redata.example.test", api_key=None, session=mock.Mock())
