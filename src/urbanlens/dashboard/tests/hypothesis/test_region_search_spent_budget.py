"""A spent Nominatim budget is a "try again" answer from the region search, not a 500."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.core.rate_limiter import (
    EnvironmentRefusedError,
    RateLimitExceededError,
    UpstreamThrottledError,
)

_SEARCH = "urbanlens.dashboard.controllers.region_search.NominatimGateway.search"


class RegionSearchSpentBudgetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.client.force_login(baker.make(User))

    def _get(self, error: Exception):
        with mock.patch(_SEARCH, side_effect=error):
            return self.client.get(reverse("region_search.search"), {"q": "Albany, NY"})

    def test_a_spent_budget_answers_429_with_a_retry_after(self) -> None:
        response = self._get(RateLimitExceededError("nominatim"))

        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertIn("error", response.json())

    def test_an_upstream_throttle_passes_its_own_wait_on(self) -> None:
        response = self._get(UpstreamThrottledError("nominatim", retry_after=42))

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "42")

    def test_an_environment_that_does_not_call_nominatim_says_so(self) -> None:
        response = self._get(EnvironmentRefusedError("nominatim", category="free", environment="development"))

        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())
