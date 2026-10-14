"""An outage must not be cached as "there is nothing here"."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _site(**fields) -> Location:
    return baker.make(
        Location,
        latitude=41.7321,
        longitude=-73.9262,
        official_name="Hudson River State Hospital",
        official_name_source="google_places",
        **fields,
    )


class MediaProviderPartialOutageTests(TestCase):
    """One of a provider's queries failing does not stop the others' results being stored."""

    def _provider(self, outcomes: dict[str, Exception | list[str]]):
        from dataclasses import dataclass

        from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider

        @dataclass(slots=True, kw_only=True)
        class Provider(MediaProvider):
            service_key = "outage_probe"

            def _generate_media(self, search_term: str, address: str | None = None):
                outcome = outcomes[search_term]
                if isinstance(outcome, Exception):
                    raise outcome
                yield from (MediaItem(url=url, thumb_url=url, caption="", source="probe") for url in outcome)

        return Provider()

    def _rows(self, location: Location) -> int:
        return LocationCache.objects.filter(location=location, source="outage_probe").count()

    def test_an_outage_on_one_query_and_results_on_another_are_cached_briefly(self) -> None:
        location = _site()
        provider = self._provider({"a": requests.ConnectionError("refused"), "b": ["https://example.test/1"]})

        items, _from_cache = provider.get_media(location, ["a", "b"])

        self.assertEqual(len(items), 1)
        self.assertEqual(self._rows(location), 1)
        row = LocationCache.objects.get(location=location, source="outage_probe")
        self.assertEqual(row.data.get(UNANSWERED_SOURCES_KEY), ["outage_probe"])
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNone(LocationCache.get_fresh(location, "outage_probe"))

    def test_results_from_every_query_keep_the_window(self) -> None:
        location = _site()
        provider = self._provider({"a": ["https://example.test/1"], "b": []})

        provider.get_media(location, ["a", "b"])

        row = LocationCache.objects.get(location=location, source="outage_probe")
        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)

    def test_an_outage_on_one_query_and_nothing_on_another_is_not_cached(self) -> None:
        location = _site()
        provider = self._provider({"a": requests.ConnectionError("refused"), "b": []})

        with self.assertRaises(requests.ConnectionError):
            provider.get_media(location, ["a", "b"])

        self.assertEqual(self._rows(location), 0)

    def test_a_failure_that_is_not_an_outage_is_still_an_answer(self) -> None:
        location = _site()
        provider = self._provider({"a": ValueError("unparseable")})

        provider.get_media(location, ["a"])

        self.assertEqual(self._rows(location), 1)


class SourceOutageClassificationTests(SimpleTestCase):
    """What :func:`is_source_outage` calls "could not ask", which every cache write above depends on."""

    def _response(self, status: int) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        return response

    def test_could_not_ask(self) -> None:
        from urbanlens.dashboard.services.core.gateway import is_source_outage
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        for exc in (
            requests.ConnectionError("refused"),
            requests.Timeout("timed out"),
            ConnectionRefusedError(),
            TimeoutError(),
            requests.HTTPError(response=self._response(503)),
            requests.HTTPError(response=self._response(429)),
            requests.HTTPError("no response"),
            GatewayRequestError("Could not reach the upstream"),
            RateLimitExceededError("nominatim"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertTrue(is_source_outage(exc))

    def test_answered(self) -> None:
        from urbanlens.dashboard.services.core.gateway import is_source_outage

        for exc in (
            requests.HTTPError(response=self._response(404)),
            requests.HTTPError(response=self._response(400)),
            ValueError("unparseable"),
            KeyError("results"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_source_outage(exc))


class AddressBackfillOutageTests(TestCase):
    """The address backfill's marker means "attempted, ever": an outage must not set it."""

    _GEOCODE = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.geocode_coordinates"
    _ADMIN = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway.reverse_geocode_admin"

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=41.7321, longitude=-73.9262, route=None, locality=None)

    def _enrich(self, *, geocode: dict, admin: dict) -> None:
        from urbanlens.dashboard.services.locations.enrichment import AddressEnrichmentSource

        with (
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "key"),
            mock.patch(self._GEOCODE, **geocode),
            mock.patch(self._ADMIN, **admin),
        ):
            AddressEnrichmentSource().enrich(self.location)

    def _marked(self) -> bool:
        return LocationCache.objects.filter(location=self.location, source="address_backfill").exists()

    def test_an_outage_of_both_sets_no_marker(self) -> None:
        with self.assertRaises(requests.ConnectionError):
            self._enrich(
                geocode={"side_effect": requests.ConnectionError("refused")},
                admin={"side_effect": requests.ConnectionError("refused")},
            )

        self.assertFalse(self._marked())

    def test_a_google_outage_sets_no_marker_though_openstreetmap_filled_the_town(self) -> None:
        """The street only comes from Google, so the marker would end the one chance of getting it."""
        with self.assertRaises(requests.ConnectionError):
            self._enrich(
                geocode={"side_effect": requests.ConnectionError("refused")},
                admin={"return_value": {"city": "Poughkeepsie", "state": "New York", "country": "US"}},
            )

        self.location.refresh_from_db()
        self.assertEqual(self.location.locality, "Poughkeepsie")
        self.assertFalse(self._marked())

    def test_nothing_found_by_either_sets_the_marker(self) -> None:
        self._enrich(geocode={"return_value": {"results": []}}, admin={"return_value": None})

        self.assertTrue(self._marked())
