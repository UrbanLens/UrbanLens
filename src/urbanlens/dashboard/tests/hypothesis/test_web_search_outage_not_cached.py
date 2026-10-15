"""A web search outage must not be cached as "there is nothing here"."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.points import WEB_SEARCH_PROVIDERS
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.search.search import WebSearchProvider


class _Search(WebSearchProvider):
    """Answers with ``results`` or raises ``failure``."""

    key = "fake_search"

    def __init__(self, results: list[dict[str, Any]] | None = None, failure: Exception | None = None) -> None:
        self.results = results or []
        self.failure = failure

    def available(self) -> bool:
        return True

    def search_web(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        if self.failure is not None:
            raise self.failure
        return self.results


class WebSearchOutageTests(TestCase):
    """The page's own fetch shows "try later" and stores nothing; the warming task stores nothing either."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        grant_subscription(self.user, baker.make(SubscriptionRole, features=SiteFeature.SEARCH), self.user, None)
        location = baker.make(
            Location,
            latitude=41.7321,
            longitude=-73.9262,
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            country="US",
        )
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, name="Hudson River")

    def _rows(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source="web_search").count()

    def _view(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        request = RequestFactory().get("/")
        request.user = self.user
        return PinController().web_search(request, pin_slug=self.pin.slug)

    def test_the_page_says_try_later_and_stores_nothing(self) -> None:
        with WEB_SEARCH_PROVIDERS.overridden([_Search(failure=ProviderUnavailableError("every source failed"))]):
            response = self._view()

        self.assertContains(response, "Search unavailable")
        self.assertEqual(self._rows(), 0)

    def test_the_warming_task_stores_nothing(self) -> None:
        from urbanlens.dashboard.tasks import refresh_pin_web_search

        with WEB_SEARCH_PROVIDERS.overridden([_Search(failure=ProviderUnavailableError("refused"))]):
            refresh_pin_web_search.apply(args=[self.pin.pk])

        self.assertEqual(self._rows(), 0)

    def test_a_genuine_empty_answer_is_cached(self) -> None:
        with WEB_SEARCH_PROVIDERS.overridden([_Search([])]):
            self._view()

        self.assertGreater(self._rows(), 0)
