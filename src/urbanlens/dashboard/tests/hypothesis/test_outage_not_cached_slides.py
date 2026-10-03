"""A slide provider that couldn't reach its source caches nothing; one that was answered caches the answer (P214)."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.esri import EsriGateway
from urbanlens.dashboard.services.apis.locations.google.maps import (
    GoogleMapsGateway,
    StreetViewNotFoundError,
    StreetViewStatusError,
)
from urbanlens.dashboard.services.core.gateway import is_source_outage


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


def _metadata(status: str) -> mock.Mock:
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = {"status": status}
    return response


class _SlideCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.session = mock.Mock()
        self.gateway = GoogleMapsGateway(api_key="test-key", session=self.session)


class GoogleSatelliteTests(_SlideCase):
    def _fetch_twice(self, failure: BaseException) -> tuple:
        self.session.get.side_effect = failure
        first = self.gateway.get_satellite_slides(41.72, -73.93)
        second = self.gateway.get_satellite_slides(41.72, -73.93)
        return first, second

    def test_a_connection_failure_is_degraded_and_not_cached(self) -> None:
        first, second = self._fetch_twice(requests.ConnectionError("refused"))

        self.assertTrue(first.degraded)
        self.assertFalse(second.from_cache)

    def test_a_timeout_is_degraded_and_not_cached(self) -> None:
        first, second = self._fetch_twice(requests.Timeout("slow"))

        self.assertTrue(first.degraded)
        self.assertFalse(second.from_cache)

    def test_a_503_is_degraded_and_not_cached(self) -> None:
        response = mock.Mock()
        response.raise_for_status.side_effect = _http_error(503)
        first, second = self._fetch_twice_with(response)

        self.assertTrue(first.degraded)
        self.assertFalse(second.from_cache)

    def test_a_refusal_is_an_answer_and_is_cached(self) -> None:
        response = mock.Mock()
        response.raise_for_status.side_effect = _http_error(403)
        first, second = self._fetch_twice_with(response)

        self.assertFalse(first.degraded)
        self.assertEqual(first.slides, [])
        self.assertTrue(second.from_cache)

    def _fetch_twice_with(self, response: mock.Mock) -> tuple:
        self.session.get.return_value = response
        return self.gateway.get_satellite_slides(41.72, -73.93), self.gateway.get_satellite_slides(41.72, -73.93)


class GoogleStreetViewTests(_SlideCase):
    def test_no_panorama_in_range_is_an_answer_and_is_cached(self) -> None:
        self.session.get.return_value = _metadata("ZERO_RESULTS")

        first = self.gateway.get_street_view_slides(41.72, -73.93)
        second = self.gateway.get_street_view_slides(41.72, -73.93)

        self.assertFalse(first.degraded)
        self.assertEqual(first.slides, [])
        self.assertTrue(second.from_cache)

    def test_an_exhausted_quota_is_degraded_and_not_cached(self) -> None:
        self.session.get.return_value = _metadata("OVER_QUERY_LIMIT")

        first = self.gateway.get_street_view_slides(41.72, -73.93)
        second = self.gateway.get_street_view_slides(41.72, -73.93)

        self.assertTrue(first.degraded)
        self.assertFalse(second.from_cache)

    def test_a_connection_failure_is_degraded_and_not_cached(self) -> None:
        self.session.get.side_effect = requests.ConnectionError("refused")

        first = self.gateway.get_street_view_slides(41.72, -73.93)

        self.assertTrue(first.degraded)
        self.assertFalse(self.gateway.get_street_view_slides(41.72, -73.93).from_cache)

    def test_a_denied_request_is_an_answer(self) -> None:
        self.session.get.return_value = _metadata("REQUEST_DENIED")

        first = self.gateway.get_street_view_slides(41.72, -73.93)

        self.assertFalse(first.degraded)
        self.assertTrue(self.gateway.get_street_view_slides(41.72, -73.93).from_cache)


class EsriWaybackTests(_SlideCase):
    """The current Esri slides are URL templates; only the Wayback release list is fetched."""

    def setUp(self) -> None:
        super().setUp()
        self.esri = EsriGateway(session=self.session)

    def _fetch_twice(self) -> tuple:
        return self.esri.get_satellite_slides(41.72, -73.93), self.esri.get_satellite_slides(41.72, -73.93)

    def test_an_unreachable_release_list_is_degraded_and_not_cached(self) -> None:
        for failure in (requests.ConnectionError("refused"), requests.Timeout("slow")):
            with self.subTest(failure=type(failure).__name__):
                cache.clear()
                self.session.get.side_effect = failure
                first, second = self._fetch_twice()

                self.assertTrue(first.degraded)
                self.assertEqual([slide.source for slide in first.slides], ["Esri World Imagery", "USGS National Map"])
                self.assertFalse(second.from_cache)

    def test_a_503_release_list_is_degraded_and_not_cached(self) -> None:
        response = mock.Mock(ok=False, status_code=503, text="")
        response.raise_for_status.side_effect = _http_error(503)
        self.session.get.return_value = response
        first, second = self._fetch_twice()

        self.assertTrue(first.degraded)
        self.assertFalse(second.from_cache)

    def test_a_missing_release_list_is_an_answer_and_is_cached(self) -> None:
        response = mock.Mock(ok=False, status_code=404, text="")
        response.raise_for_status.side_effect = _http_error(404)
        self.session.get.return_value = response
        first, second = self._fetch_twice()

        self.assertFalse(first.degraded)
        self.assertTrue(second.from_cache)


class StreetViewErrorTests(SimpleTestCase):
    """Callers outside the carousel (photo backfill) tell an outage from an answer by ``is_source_outage``."""

    def test_quota_and_unknown_errors_are_outages(self) -> None:
        for status in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"):
            with self.subTest(status=status):
                self.assertTrue(is_source_outage(StreetViewStatusError(status)))

    def test_a_denied_or_invalid_request_is_not(self) -> None:
        for status in ("REQUEST_DENIED", "INVALID_REQUEST"):
            with self.subTest(status=status):
                self.assertFalse(is_source_outage(StreetViewStatusError(status)))

    def test_no_imagery_is_not_an_outage(self) -> None:
        self.assertFalse(is_source_outage(StreetViewNotFoundError("none")))

    def test_both_are_still_value_errors_for_existing_callers(self) -> None:
        self.assertIsInstance(StreetViewStatusError("REQUEST_DENIED"), ValueError)
        self.assertIsInstance(StreetViewNotFoundError("none"), ValueError)
