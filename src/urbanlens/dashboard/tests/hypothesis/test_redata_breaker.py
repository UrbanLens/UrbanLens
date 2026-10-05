"""A throttled REData is left alone until the wait it named has passed.

Reproduces the 2026-09-23 HRSH run: REData throttled the key's lookup budget at 20:02 ("Expected
available in 989 seconds") and the stack kept calling - 1,041 of 1,439 REData calls in that hour
failed, and every refused call was charged to the key's default budget as well.
"""

from __future__ import annotations

import json
import time
from unittest import mock

from django.core.cache import cache
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsBusyError, RedataGateway
from urbanlens.dashboard.services.core.gateway import GatewayRateLimitedError, GatewayRequestError, UpstreamBusyError
from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError, _RateLimitedSession
from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker

_BASE = "https://redata.example.test/api/v1/"
_NEARBY = f"{_BASE}places/search/nearby/"
_PARCEL = f"{_BASE}parcels/lookup/"
_CAPABILITIES = f"{_BASE}capabilities/"
_PARKS_NEARBY = f"{_BASE}parks/nearby/"
_TILE = f"{_BASE}tiles/street/14/4823/6160/"
_THROTTLED = {"error": "throttled", "message": "Request was throttled. Expected available in 989 seconds."}


def _response(status_code: int, body: dict | None = None, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, headers=headers or {}, ok=200 <= status_code < 300)
    response.json.return_value = body or {}
    response.text = json.dumps(body or {})
    return response


def _session(service: str, *responses: mock.Mock) -> _RateLimitedSession:
    session = _RateLimitedSession(service)
    session._session = mock.Mock()
    session._session.request.side_effect = list(responses)
    return session


class ThrottledKeyShortCircuitsTests(TestCase):
    def test_a_429_stops_the_next_lookup_call_going_out(self) -> None:
        places = _session("redata_places", _response(429, _THROTTLED, {"Retry-After": "989"}))
        places.get(_NEARBY)
        parcels = _session("redata_api")

        with pytest.raises(UpstreamThrottledError) as caught:
            parcels.get(_PARCEL)

        parcels._session.request.assert_not_called()
        self.assertGreaterEqual(caught.value.retry_after, 988)
        self.assertLessEqual(caught.value.retry_after, 989)

    def test_the_refusal_is_logged_as_rate_limited(self) -> None:
        _session("redata_places", _response(429, _THROTTLED, {"Retry-After": "60"})).get(_NEARBY)

        with pytest.raises(UpstreamThrottledError):
            _session("redata_api").get(_PARCEL)

        row = ApiCallLog.objects.get(service="redata_api")
        self.assertTrue(row.was_rate_limited)
        self.assertFalse(row.success)
        self.assertEqual(row.endpoint, _PARCEL)

    def test_the_refusal_is_every_error_callers_already_handle(self) -> None:
        _session("redata_places", _response(429, _THROTTLED)).get(_NEARBY)

        with pytest.raises(UpstreamThrottledError) as caught:
            _session("redata_places").get(_NEARBY)

        self.assertIsInstance(caught.value, GatewayRequestError)
        self.assertIsInstance(caught.value, UpstreamBusyError)
        self.assertIsInstance(caught.value, GatewayRateLimitedError)

    def test_the_wait_is_read_from_the_body_without_a_header(self) -> None:
        _session("redata_places", _response(429, _THROTTLED)).get(_NEARBY)

        with pytest.raises(UpstreamThrottledError) as caught:
            _session("redata_places").get(_NEARBY)

        self.assertGreaterEqual(caught.value.retry_after, 988)

    def test_calls_go_out_again_once_the_wait_has_passed(self) -> None:
        _session("redata_places", _response(429, _THROTTLED, {"Retry-After": "30"})).get(_NEARBY)
        later = _session("redata_places", _response(200, {"results": []}))

        with mock.patch("urbanlens.dashboard.services.core.upstream_breaker.time.time", return_value=time.time() + 31):
            later.get(_NEARBY)

        later._session.request.assert_called_once()

    def test_a_shorter_wait_does_not_shorten_a_longer_one(self) -> None:
        _session("redata_places", _response(429, _THROTTLED, {"Retry-After": "900"})).get(_NEARBY)
        RedataBreaker().trip("pool:lookup", 5)

        with pytest.raises(UpstreamThrottledError) as caught:
            _session("redata_places").get(_NEARBY)

        self.assertGreater(caught.value.retry_after, 800)


class ThrottlePoolsTests(TestCase):
    """REData's pools are separate budgets; a throttle on one leaves the others callable."""

    def test_a_lookup_throttle_leaves_default_only_endpoints_callable(self) -> None:
        _session("redata_places", _response(429, _THROTTLED)).get(_NEARBY)

        for service, url in (("redata_capabilities", _CAPABILITIES), ("redata_national_parks", _PARKS_NEARBY)):
            with self.subTest(url=url):
                session = _session(service, _response(200, {}))
                session.get(url)
                session._session.request.assert_called_once()

    def test_a_default_budget_throttle_stops_lookup_calls_too(self) -> None:
        _session("redata_capabilities", _response(429, _THROTTLED)).get(_CAPABILITIES)

        with pytest.raises(UpstreamThrottledError):
            _session("redata_api").get(_PARCEL)

    def test_tiles_have_their_own_pool(self) -> None:
        _session("redata_capabilities", _response(429, _THROTTLED)).get(_CAPABILITIES)
        tiles = _session("redata_basemap_tiles", _response(200, {}))

        tiles.get(_TILE)

        tiles._session.request.assert_called_once()


class BusySourceTests(TestCase):
    """A 503 ``rate_limited`` is one of REData's own sources out of budget, not the key."""

    def test_it_stops_calls_to_that_endpoint(self) -> None:
        busy = {"error": "rate_limited", "message": "Places API (New) request budget is exhausted right now."}
        _session("redata_places", _response(503, busy)).get(_NEARBY)

        with pytest.raises(UpstreamThrottledError) as caught:
            _session("redata_places").get(_NEARBY)

        self.assertGreaterEqual(caught.value.retry_after, 59)

    def test_it_leaves_other_endpoints_callable(self) -> None:
        busy = {"error": "rate_limited", "message": "Places API (New) request budget is exhausted right now."}
        _session("redata_places", _response(503, busy)).get(_NEARBY)
        parcels = _session("redata_api", _response(200, {}))

        parcels.get(_PARCEL)

        parcels._session.request.assert_called_once()

    def test_one_providers_budget_leaves_its_siblings_callable(self) -> None:
        """Street view is asked once per provider; KartaView out of budget says nothing about Panoramax."""
        busy = {"error": "rate_limited", "message": "kartaview: kartaview request budget is exhausted right now."}
        timeline = f"{_BASE}street-view/timeline/"
        _session("redata_street_view", _response(503, busy)).get(timeline, params={"provider": "kartaview"})
        panoramax = _session("redata_street_view", _response(200, {}))

        panoramax.get(timeline, params={"provider": "panoramax"})

        panoramax._session.request.assert_called_once()
        with pytest.raises(UpstreamThrottledError):
            _session("redata_street_view").get(timeline, params={"provider": "kartaview"})

    def test_a_provider_refusing_redata_stops_calls_to_that_endpoint(self) -> None:
        """REData answers so when Google or GDELT throttles it, without a wait; on dev, 53 such calls in two days."""
        refusals = (
            (_NEARBY, {"error": "places_api_unavailable", "message": "Places API (New) answered 429 for ..."}),
            (
                f"{_BASE}search/news/",
                {"error": "search_unavailable", "message": "GDELT request failed with status 429"},
            ),
        )
        for url, body in refusals:
            with self.subTest(body["error"]):
                _session("redata_places", _response(503, body)).get(url)

                with pytest.raises(UpstreamThrottledError):
                    _session("redata_places").get(url)

    def test_one_bad_place_id_leaves_every_other_place_callable(self) -> None:
        """REData answers Google's 400 for a malformed id with the same code; any signed-in user can send one."""
        bad_id = {
            "error": "places_api_unavailable",
            "message": "Places API (New) answered 400 for place_id 'bogus1': ...",
        }
        _session("redata_places", _response(503, bad_id)).get(f"{_BASE}places/bogus1/")
        other = _session("redata_places", _response(200, {}))

        other.get(f"{_BASE}places/ChIJ2eUgeAK6j4AR/")

        other._session.request.assert_called_once()

    def test_an_image_search_failure_leaves_web_search_callable(self) -> None:
        """Image search is asked of other providers on the same endpoint, chosen by ``images``."""
        web = f"{_BASE}search/web/"
        refused = {"error": "search_unavailable", "message": "Image provider answered 503"}
        _session("redata_search_web", _response(503, refused)).get(web, params={"q": "mill", "images": "true"})
        text = _session("redata_search_web", _response(200, {}))

        text.get(web, params={"q": "mill"})

        text._session.request.assert_called_once()

    def test_a_named_provider_redata_could_not_reach_stops_calls_to_it(self) -> None:
        """REData waited 20 s on Chronicling America for every pin page; the other archives answered."""
        search = f"{_BASE}reference-documents/search/"
        for status in ("unavailable", "rate_limited"):
            with self.subTest(status):
                cache.clear()
                unanswered = {
                    "count": 0,
                    "complete": False,
                    "results": [],
                    "providers": [
                        {"provider": "internet_archive", "status": "skipped"},
                        {
                            "provider": "chronicling_america",
                            "status": status,
                            "message": "could not be reached: ReadTimeout",
                        },
                    ],
                }
                _session("redata_reference_documents", _response(503, unanswered)).get(
                    search, params={"q": "mill", "provider": "chronicling_america"}
                )
                other = _session("redata_reference_documents", _response(200, {}))

                other.get(search, params={"q": "mill", "provider": "smithsonian"})

                other._session.request.assert_called_once()
                with pytest.raises(UpstreamThrottledError):
                    _session("redata_reference_documents").get(
                        search, params={"q": "mill", "provider": "chronicling_america"}
                    )

    def test_named_providers_given_as_a_list_trip_only_when_none_answered(self) -> None:
        """Hazard history names two providers on every call; a list reached the check as its repr and never matched."""
        hazards = f"{_BASE}hazards/"
        params = {"lat": 42.1, "lng": -73.9, "provider": ["nifc_wildfires", "fema_disasters"]}
        partly = {
            "providers": [
                {"provider": "nifc_wildfires", "status": "unavailable"},
                {"provider": "fema_disasters", "status": "ok"},
            ]
        }
        _session("redata_hazards", _response(503, partly)).get(hazards, params=params)
        again = _session("redata_hazards", _response(200, {}))

        again.get(hazards, params=params)

        again._session.request.assert_called_once()
        neither = {
            "providers": [
                {"provider": "nifc_wildfires", "status": "unavailable"},
                {"provider": "fema_disasters", "status": "rate_limited"},
            ]
        }
        _session("redata_hazards", _response(503, neither)).get(hazards, params=params)
        with pytest.raises(UpstreamThrottledError):
            _session("redata_hazards").get(hazards, params=params)

    def test_a_provider_the_request_did_not_name_trips_nothing(self) -> None:
        """REData picks a county's provider by the point, so the next point may be another county's."""
        unanswered = {
            "count": 0,
            "complete": False,
            "providers": [{"provider": "dutchess_gis", "status": "unavailable"}],
        }
        _session("redata_api", _response(503, unanswered)).get(_PARCEL)
        again = _session("redata_api", _response(200, {}))

        again.get(_PARCEL)

        again._session.request.assert_called_once()

    def test_a_503_about_one_place_trips_nothing(self) -> None:
        """``source_rate_limited`` is one county's scraper; the next point may be in another county."""
        _session("redata_api", _response(503, {"error": "source_rate_limited", "message": "county"})).get(_PARCEL)
        again = _session("redata_api", _response(200, {}))

        again.get(_PARCEL)

        again._session.request.assert_called_once()


class OtherServicesTests(TestCase):
    def test_a_non_redata_429_trips_nothing(self) -> None:
        _session("overpass", _response(429, _THROTTLED)).get("https://overpass.example.test/api/interpreter")
        again = _session("overpass", _response(200, {}))

        again.get("https://overpass.example.test/api/interpreter")

        again._session.request.assert_called_once()

    def test_an_unreadable_cache_lets_the_call_go_out(self) -> None:
        session = _session("redata_places", _response(200, {}))

        with mock.patch(
            "urbanlens.dashboard.services.core.upstream_breaker.cache.get_many", side_effect=ConnectionError
        ):
            session.get(_NEARBY)

        session._session.request.assert_called_once()


class PropertyRecordsGatewayTests(TestCase):
    """RedataGateway's callers catch its own error types, so the refusal must arrive as one."""

    def test_a_short_circuited_download_is_busy_with_the_wait(self) -> None:
        RedataBreaker().trip("pool:lookup", 600)
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key")
        gateway.session._session = mock.Mock()

        with pytest.raises(PropertyRecordsBusyError) as caught:
            gateway.download_cultural_resource_attachment("54d64e82-04d4-438c-8003-056aa3480754", 480501)

        gateway.session._session.request.assert_not_called()
        self.assertGreaterEqual(caught.value.retry_after, 599)

    def test_a_short_circuited_lookup_is_a_property_records_error(self) -> None:
        RedataBreaker().trip("pool:lookup", 600)
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key")

        with pytest.raises(PropertyRecordsBusyError):
            gateway.lookup_parcel(42.0, -73.0)

    def test_a_429_on_a_lookup_is_busy_not_a_source_error(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(429, _THROTTLED)
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="test-key", session=session)

        with pytest.raises(PropertyRecordsBusyError):
            gateway.lookup_parcel(42.0, -73.0)


class LocationContextGatewayTests(TestCase):
    """Its callers catch LocationContextUnavailableError, which is what a 429 used to arrive as."""

    def test_a_short_circuited_call_is_a_location_context_error(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
        from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway

        RedataBreaker().trip("pool:lookup", 300)
        gateway = RedataHazardsGateway(base_url="https://redata.example.test", api_key="k")

        with pytest.raises(LocationContextUnavailableError) as caught:
            gateway.get_hazard_events(41.73, -73.92)

        self.assertIsInstance(caught.value, UpstreamBusyError)
        self.assertGreaterEqual(caught.value.retry_after, 299)

    def test_a_429_is_busy(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway

        session = mock.Mock()
        session.get.return_value = _response(429, _THROTTLED, {"Retry-After": "120"})
        gateway = RedataHazardsGateway(base_url="https://redata.example.test", api_key="k", session=session)

        with pytest.raises(UpstreamBusyError) as caught:
            gateway.get_hazard_events(41.73, -73.92)

        self.assertEqual(caught.value.retry_after, 120)
