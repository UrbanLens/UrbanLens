"""A ``/maps/`` answer REData marked ``complete: false`` is shown but not kept as the whole answer.

REData's 8 s deadline can cut off the nearby-maps read; the containing maps still come back. Caching that for
24 h (browse) or ``external_data_cache_days`` (Photos tab) hid the rest of the sheets until the entry lapsed. A body
without the ``complete`` key comes from an older REData and is complete.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock
import uuid as uuid_module

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import UNANSWERED_SOURCES_KEY, LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import (
    PARTIAL_MAPS_STALE_AFTER,
    MapMatches,
    RedataHistoricalMapsGateway,
    maps_answer_complete,
)
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway.RedataHistoricalMapsGateway"
_LOOKUP = f"{_GATEWAY_PATH}.get_maps_covering"
_CONFIGURED_PATH = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"


def _match(georeference_uuid: str = "") -> dict:
    return {
        "sheet": {
            "title": "Sanborn Fire Insurance Map",
            "date_text": "1893",
            "kind": "fire_insurance",
            "thumbnail_url": "https://example.test/t.jpg",
        },
        "georeference": {
            "uuid": georeference_uuid or str(uuid_module.uuid4()),
            "bounds": [-71.06, 42.35, -71.05, 42.36],
        },
        "contains_point": True,
    }


def _answer(matches: list[dict], *, complete: bool | None) -> MapMatches:
    answer = MapMatches(matches)
    if complete is not None:
        answer.complete = complete
    return answer


class GatewayCompleteFlagTests(SimpleTestCase):
    def _get(self, body: dict) -> list:
        response = mock.Mock(status_code=200)
        response.json.return_value = body
        session = mock.Mock()
        session.get.return_value = response
        gateway = RedataHistoricalMapsGateway(base_url="https://redata.example.test", api_key="k", session=session)
        return gateway.get_maps_covering(42.355, -71.055)

    def test_a_complete_answer_is_complete(self) -> None:
        answer = self._get({"results": [_match()], "complete": True})

        self.assertEqual(len(answer), 1)
        self.assertTrue(maps_answer_complete(answer))

    def test_a_partial_answer_keeps_its_matches_and_says_it_is_partial(self) -> None:
        answer = self._get({"results": [_match()], "complete": False})

        self.assertEqual(len(answer), 1)
        self.assertFalse(maps_answer_complete(answer))

    def test_a_missing_key_means_complete(self) -> None:
        answer = self._get({"results": [_match()]})

        self.assertTrue(maps_answer_complete(answer))

    def test_a_plain_list_counts_as_complete(self) -> None:
        self.assertTrue(maps_answer_complete([_match()]))


class BrowseCachingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        request_upstreams.HistoricalMapsBrowseUpstream.reset()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=42.355, longitude=-71.055)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile, location=location)
        self.url = reverse("pin.overlays.historical", args=[self.pin.slug])
        self.sheet_uuid = str(uuid_module.uuid4())
        patcher = mock.patch(_CONFIGURED_PATH, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _browse_twice(self, first: list, second: list) -> tuple[str, str, int]:
        with mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.get_maps_covering.side_effect = [first, second]
            one = self.client.get(self.url).content.decode()
            two = self.client.get(self.url).content.decode()
            return one, two, gateway_cls.return_value.get_maps_covering.call_count

    def test_a_partial_answer_is_shown_and_asked_again(self) -> None:
        containing = _match()
        full = _match(self.sheet_uuid)

        first, second, calls = self._browse_twice(
            _answer([containing], complete=False), _answer([containing, full], complete=True)
        )

        self.assertIn(containing["georeference"]["uuid"], first)
        self.assertNotIn(self.sheet_uuid, first)
        self.assertIn(self.sheet_uuid, second)
        self.assertEqual(calls, 2)

    def test_a_complete_answer_is_cached(self) -> None:
        _first, _second, calls = self._browse_twice(_answer([_match()], complete=True), _answer([], complete=True))

        self.assertEqual(calls, 1)

    def test_an_answer_without_the_key_is_cached(self) -> None:
        _first, _second, calls = self._browse_twice(_answer([_match()], complete=None), _answer([], complete=None))

        self.assertEqual(calls, 1)


class PhotosTabCachingTests(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.73, longitude=-73.92)
        self.pin = baker.make(Pin, profile=profile, location=location, parent_pin=None, name="HRSH")

    def _fetch(self, answer: list) -> LocationCache:
        from urbanlens.dashboard.plugins.builtin.redata_historical_map_media import HistoricalMapMediaSource

        source = HistoricalMapMediaSource()
        with mock.patch(_LOOKUP, return_value=answer):
            source.fetch(self.pin)
        return LocationCache.objects.get(location=self.pin.location, source=source.cache_source)

    def _fresh_for(self, row: LocationCache) -> timedelta:
        """How much longer the row stays fresh under the site-wide window."""
        return row.updated - LocationCache.fresh_since()

    def test_a_complete_answer_is_kept_for_the_site_window(self) -> None:
        row = self._fetch(_answer([_match()], complete=True))

        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        self.assertGreater(self._fresh_for(row), PARTIAL_MAPS_STALE_AFTER * 100)

    def test_an_answer_without_the_key_is_kept_for_the_site_window(self) -> None:
        row = self._fetch([_match()])

        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        self.assertGreater(self._fresh_for(row), PARTIAL_MAPS_STALE_AFTER * 100)

    def test_a_partial_answer_is_shown_but_goes_stale_within_minutes(self) -> None:
        row = self._fetch(_answer([_match()], complete=False))

        self.assertEqual(len(row.data["maps"]), 1)
        self.assertTrue(row.data[UNANSWERED_SOURCES_KEY])
        self.assertLessEqual(self._fresh_for(row), PARTIAL_MAPS_STALE_AFTER + timedelta(seconds=5))
        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, row.source))
