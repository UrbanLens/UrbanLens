"""The global-search panel takes whatever the client types, as fast as it types it.

Two findings on the same view (N21 H42 and H49). The panel is fired per
keystroke, and each fire hands one query to ~11 providers, each running a
trigram-similarity scan over the account's rows that no index helps. The same
engine's public API surface caps the query at 250 characters specifically to
keep a long needle out of that per-row comparison - the web view, which is the
one people actually type into, had neither the cap nor a throttle.

Truncated rather than refused: somebody who pastes a paragraph into a search box
should get the results for the first part of it, not an error.
"""

from __future__ import annotations

from unittest import mock

from django.urls import resolve, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.search import GLOBAL_SEARCH_RATE, MAX_QUERY_LENGTH
from urbanlens.dashboard.models.profile.model import Profile

_ENGINE = "urbanlens.dashboard.controllers.search.GlobalSearchEngine.search"


class _PanelCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        from django.core.cache import cache

        cache.clear()  # the throttle counts in the cache, which outlives a test
        self.addCleanup(cache.clear)
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("search.panel")

    def _query_the_engine_saw(self, sent: str) -> str:
        with mock.patch(_ENGINE) as search:
            response = self.client.get(self.url, {"q": sent})
        self.assertEqual(response.status_code, 200)
        search.assert_called_once()
        return search.call_args.args[1]


class TheQueryLengthIsCappedTests(_PanelCase):
    def test_the_cap_matches_the_one_the_api_already_applies(self) -> None:
        from urbanlens.dashboard.external_api import serializers_search

        self.assertEqual(MAX_QUERY_LENGTH, serializers_search.MAX_QUERY_LENGTH)

    def test_a_long_needle_never_reaches_the_providers(self) -> None:
        seen = self._query_the_engine_saw("a" * (MAX_QUERY_LENGTH + 500))

        self.assertEqual(len(seen), MAX_QUERY_LENGTH)

    def test_an_ordinary_query_is_passed_through_untouched(self) -> None:
        """The half that stops the test above passing against a view that searches nothing."""
        seen = self._query_the_engine_saw("abandoned mill")

        self.assertEqual(seen, "abandoned mill")


class ThePanelIsThrottledTests(_PanelCase):
    def test_the_view_is_behind_the_throttle(self) -> None:
        view = resolve(self.url).func

        self.assertEqual(getattr(view, "throttle_scope", None), "search.panel", "the search panel is not throttled")
        self.assertEqual(getattr(view, "throttle_rate", None), GLOBAL_SEARCH_RATE)

    def test_the_throttle_counts_the_method_the_panel_uses(self) -> None:
        """Its expensive method is GET, which the throttle does not count by default."""
        self.assertIn("GET", getattr(resolve(self.url).func, "throttle_methods", frozenset()))

    def test_the_rate_refuses_once_its_budget_is_spent(self) -> None:
        """Asserted against the rate rather than by issuing the requests.

        A storm of `limit + 5` real requests renders that many pages, which takes
        longer than the rate's own window, so the counter rolls over halfway
        through and nothing is ever refused - the first version of this test
        passed against a correctly throttled view for that reason.
        """
        from urbanlens.dashboard.services.security.throttle import allow

        verdicts = [
            allow("search.panel.test", "user:1", GLOBAL_SEARCH_RATE) for _ in range(GLOBAL_SEARCH_RATE.limit + 2)
        ]

        self.assertTrue(all(verdicts[: GLOBAL_SEARCH_RATE.limit]), "the budget was spent before its limit")
        self.assertFalse(verdicts[-1], "the budget never ran out")

    def test_one_search_is_not_refused(self) -> None:
        """The anti-vacuity half: a throttle that refuses everything would pass above."""
        with mock.patch(_ENGINE):
            self.assertEqual(self.client.get(self.url, {"q": "mill"}).status_code, 200)
