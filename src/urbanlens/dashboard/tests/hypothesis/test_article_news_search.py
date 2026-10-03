"""Article > News searches the web for the place by its name and town, and HRSH's search finds it (P235).

Article > News is the pin page's web search (``pin.web_search?surface=article``). The fixture holds REData's recorded
``/search/web/`` answers for the Hudson River State Hospital campus pin's shared search: the query the page sent
before the fix, which every engine answered with nothing, and the one it sends now.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway
from urbanlens.dashboard.services.search.pin_web_search import WEB_SEARCH_SOURCE, pin_web_searches
from urbanlens.dashboard.services.search.search import search_web
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hrsh_redata_web_search.json").read_text())
_BODIES = {response["params"]["q"]: response["body"] for response in _FIXTURE["responses"]}
_BEFORE, _AFTER = (response["params"]["q"] for response in _FIXTURE["responses"])


def _hrsh(**fields: Any) -> Location:
    return Location(
        latitude=Decimal("41.733280"),
        longitude=Decimal("-73.928120"),
        official_name="Hudson River State Hospital",
        official_name_source="wikipedia",
        street_number="83",
        route="Hudson View Dr",
        locality="Poughkeepsie",
        administrative_area_level_2="Dutchess County",
        administrative_area_level_1="NY",
        country="United States",
        **fields,
    )


class _RecordedResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _redata(path: str, params: dict[str, Any]) -> _RecordedResponse:
    """REData's recorded answer to a query the fixture holds; nothing else is ever asked."""
    if path != _FIXTURE["path"] or params.get("q") not in _BODIES:
        raise AssertionError(f"unrecorded REData request: {path} {params}")
    return _RecordedResponse(_BODIES[params["q"]])


class NewsQueryTests(SimpleTestCase):
    """The query names the place and its town, and requires no phrase a page about the place would not repeat."""

    def test_the_campus_search_is_its_name_and_town(self) -> None:
        searches = pin_web_searches(Pin(location=_hrsh(), name=""))
        self.assertEqual([search.query for search in searches], [_AFTER])

    def test_a_place_named_by_its_address_keeps_its_town_as_a_phrase(self) -> None:
        """A street address alone matches the same address in every other town."""
        location = Location(
            latitude=Decimal("39.1"),
            longitude=Decimal("-84.5"),
            official_name="118 W 9th St",
            official_name_source="google_places",
            street_number="118",
            route="W 9th St",
            locality="Cincinnati",
            administrative_area_level_1="Ohio",
            country="United States",
        )
        searches = pin_web_searches(Pin(location=location, name=""))
        self.assertEqual([search.query for search in searches], ['"118 W 9th St" "Cincinnati Ohio"'])


class RecordedNewsSearchTests(RedataConfiguredMixin, SimpleTestCase):
    """REData's real answers: the old query found nothing about HRSH, the new one finds pages about it."""

    def test_the_campus_search_returns_pages_about_the_place(self) -> None:
        query = pin_web_searches(Pin(location=_hrsh(), name=""))[0].query
        with patch.object(
            RedataSearchGateway,
            "_request",
            autospec=True,
            side_effect=lambda _self, path, params: _redata(path, params),
        ):
            results = search_web(query)
        self.assertEqual(len(results), 10)
        self.assertTrue(
            all("hudson river state hospital" in f"{r['title']} {r['snippet']}".casefold() for r in results[:5])
        )

    def test_the_query_sent_before_found_nothing(self) -> None:
        with patch.object(
            RedataSearchGateway,
            "_request",
            autospec=True,
            side_effect=lambda _self, path, params: _redata(path, params),
        ):
            self.assertEqual(search_web(_BEFORE), [])


class ArticleNewsTabTests(RedataConfiguredMixin, TestCase):
    """The whole path: the Article > News tab of the campus pin lists REData's results and caches them."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        grant_subscription(self.user, baker.make(SubscriptionRole, features=SiteFeature.SEARCH), self.user, None)
        location = _hrsh()
        location.save()
        self.pin = baker.make(Pin, location=location, profile=self.user.profile, name="")
        self.client.force_login(self.user)

    def test_the_news_tab_lists_the_recorded_results(self) -> None:
        with patch.object(
            RedataSearchGateway,
            "_request",
            autospec=True,
            side_effect=lambda _self, path, params: _redata(path, params),
        ):
            response = self.client.get(
                reverse("pin.web_search", kwargs={"pin_slug": self.pin.slug}), {"surface": "article"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "https://olmsted.org/blog/2025/02/14/preserving-hudson-river-state-hospital-in-poughkeepsie-ny/"
        )
        self.assertNotContains(response, "No results found.")
        row = LocationCache.objects.get(location=self.pin.location, source=WEB_SEARCH_SOURCE, audience="")
        self.assertEqual(row.query_key, _AFTER)
        self.assertEqual(len(row.data["results"]), 10)
