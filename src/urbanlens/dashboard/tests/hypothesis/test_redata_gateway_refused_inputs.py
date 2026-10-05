"""UrbanLens never spends a REData call, or REData's per-key budget, on an input REData can only refuse."""

from __future__ import annotations

from datetime import date
import math
from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.apis.locations.google.redata_places_gateway import RedataPlacesGateway
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_geocode_gateway import RedataGeocodeGateway
from urbanlens.dashboard.services.apis.locations.redata_hazards_gateway import RedataHazardsGateway
from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway
from urbanlens.dashboard.services.apis.locations.redata_weather_gateway import (
    RedataWeatherGateway,
    RedataWeatherHistoryGateway,
)
from urbanlens.dashboard.services.core.gateway import is_source_outage
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError, InputRejection

_CONFIG = {"base_url": "https://redata.example.test", "api_key": "test-key"}


def _ok(body: object) -> mock.Mock:
    response = mock.Mock(status_code=200)
    response.json.return_value = body
    response.text = ""
    return response


class LocationContextRefusalTests(TestCase):
    def _hazards(self) -> tuple[RedataHazardsGateway, mock.Mock]:
        session = mock.Mock()
        session.get.return_value = _ok({"count": 0, "complete": True, "results": [], "providers": []})
        return RedataHazardsGateway(session=session, **_CONFIG), session

    def test_impossible_points_never_reach_redata(self) -> None:
        for latitude, longitude in ((math.nan, -74.0), (91.0, 0.5), (0.0, 0.0)):
            gateway, session = self._hazards()
            with self.subTest(latitude=latitude, longitude=longitude), self.assertRaises(ImpossibleInputError):
                gateway.near_point("/api/v1/hazards/", latitude, longitude)
            session.get.assert_not_called()

    def test_the_refusal_is_the_rejected_answer_existing_callers_already_handle(self) -> None:
        gateway, _session = self._hazards()
        with self.assertRaises(LocationContextUnavailableError) as caught:
            gateway.near_point("/api/v1/hazards/", 0.0, 0.0)
        self.assertTrue(caught.exception.rejected)
        self.assertEqual(caught.exception.reason, InputRejection.NULL_ISLAND)
        self.assertFalse(is_source_outage(caught.exception))

    def test_a_radius_or_limit_that_can_hold_nothing_never_reaches_redata(self) -> None:
        for kwargs in ({"radius_meters": 0}, {"radius_meters": -10.0}, {"limit": 0}):
            gateway, session = self._hazards()
            with self.subTest(kwargs=kwargs), self.assertRaises(ImpossibleInputError):
                gateway.near_point("/api/v1/hazards/", 41.7, -73.9, **kwargs)
            session.get.assert_not_called()

    def test_weather_answers_at_sea_so_null_island_is_asked(self) -> None:
        session = mock.Mock()
        session.get.return_value = _ok({"count": 0, "complete": True, "results": [], "providers": []})
        RedataWeatherGateway(session=session, **_CONFIG).get_weather(0.0, 0.0)
        session.get.assert_called_once()

    def test_a_weather_history_range_that_ends_before_it_starts_is_refused(self) -> None:
        session = mock.Mock()
        with self.assertRaises(ImpossibleInputError) as caught:
            RedataWeatherHistoryGateway(session=session, **_CONFIG).get_history(
                41.7, -73.9, start=date(2020, 2, 1), end=date(2020, 1, 1)
            )
        self.assertIs(caught.exception.reason, InputRejection.OUT_OF_RANGE)
        session.get.assert_not_called()

    def test_a_blank_geocode_or_web_search_query_never_reaches_redata(self) -> None:
        session = mock.Mock()
        with self.assertRaises(ImpossibleInputError):
            RedataGeocodeGateway(session=session, **_CONFIG).geocode("  ")
        with self.assertRaises(ImpossibleInputError):
            RedataSearchGateway(session=session, **_CONFIG).search_web("")
        session.get.assert_not_called()

    def test_each_refusal_is_counted_under_the_gateway_s_service(self) -> None:
        gateway, _session = self._hazards()
        with self.assertRaises(ImpossibleInputError):
            gateway.near_point("/api/v1/hazards/", math.nan, 0.0)
        self.assertTrue(ApiCallLog.objects.filter(service="redata_hazards", was_rejected_input=True).exists())


class RedataPlacesRefusalTests(TestCase):
    def test_a_place_id_that_cannot_be_google_s_never_reaches_redata(self) -> None:
        session = mock.Mock()
        gateway = RedataPlacesGateway(session=session, **_CONFIG)
        for place_id in ("", "places/ChIJabc", "ChIJ abc", "cid:123", "0x89c2:0x1"):
            with self.subTest(place_id=place_id), self.assertRaises(ImpossibleInputError) as caught:
                gateway.get_place(place_id)
            self.assertIs(caught.exception.reason, InputRejection.MALFORMED_ID)
        session.get.assert_not_called()

    def test_a_short_fake_place_id_is_still_allowed(self) -> None:
        session = mock.Mock()
        session.get.return_value = _ok({"place_id": "ChIJ_example"})
        self.assertIsNotNone(RedataPlacesGateway(session=session, **_CONFIG).get_place("ChIJ_example"))

    def test_nearby_search_at_an_impossible_point_or_radius_never_reaches_redata(self) -> None:
        session = mock.Mock()
        gateway = RedataPlacesGateway(session=session, **_CONFIG)
        cases: tuple[dict[str, Any], ...] = (
            {"latitude": 0.0, "longitude": 0.0},
            {"latitude": 41.7, "longitude": -73.9, "radius_meters": 0},
            {"latitude": 41.7, "longitude": -73.9, "radius_meters": 50_001},
            {"latitude": 41.7, "longitude": -73.9, "max_results": 0},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ImpossibleInputError):
                gateway.search_nearby(**kwargs)
        session.get.assert_not_called()

    def test_blank_text_search_and_autocomplete_never_reach_redata(self) -> None:
        session = mock.Mock()
        gateway = RedataPlacesGateway(session=session, **_CONFIG)
        with self.assertRaises(ImpossibleInputError):
            gateway.search_text(" ")
        with self.assertRaises(ImpossibleInputError):
            gateway.autocomplete("")
        session.get.assert_not_called()
