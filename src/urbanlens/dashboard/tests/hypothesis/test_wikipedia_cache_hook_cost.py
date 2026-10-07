"""A location's Wikipedia match costs the same however many pins stand on it.

N29 batch 1 found the cache hook walking ``location.pins`` the first time a match gained a title, seeding an article
and adding a link on every pin there. 40d418c1d (P181) took the loop out: the hook seeds the location's wiki, and a
pin takes the match only from its own owner's activity (``wiki_seed.seed_pin_from_cached_wikipedia``). These keep
the hook's cost independent of the pin count, and keep it off the network: it reads the cached row. The wiki's
lead-image cover is not fetched, since ``_store_cover_from_url`` has no owner for a wiki cover yet (its TODO); the
egress policy is opened here so that a fetch added there fails these tests rather than being refused quietly.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki import wiki_seed

MATCH = {
    "title": "Hudson River State Hospital",
    "url": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital",
    "extract": "<p>A former psychiatric hospital.</p>",
    "thumbnail": "https://upload.wikimedia.org/hrsh.jpg",
}

#: Pins on the crowded location; the quiet one has one.
CROWD = 25


class WikipediaCacheHookCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.quiet = self._location_with_pins(1, latitude=41.73266)
        self.crowded = self._location_with_pins(CROWD, latitude=42.73266)

    @staticmethod
    def _location_with_pins(count: int, *, latitude: float) -> Location:
        location = baker.make(Location, latitude=latitude, longitude=-73.92736)
        Wiki.objects.get_or_create_for_location(location)
        for _ in range(count):
            baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)
        return location

    def _cache_a_match(self, location: Location) -> tuple[int, mock.MagicMock, mock.MagicMock]:
        """Write the match the way a lookup does and run its commit hooks.

        Any HTTP request fails the test here: the hook swallows a failed name refresh, so a request refused by the
        test network guard would otherwise go unseen.

        Returns:
            Queries the write and its hooks made, and mocks for the per-pin seed and the per-pin link.
        """
        with (
            mock.patch("urbanlens.dashboard.services.core.egress.egress_permitted", return_value=True),
            mock.patch(
                "requests.Session.send", side_effect=AssertionError("the Wikipedia cache hook made an HTTP request")
            ) as sent,
            mock.patch.object(
                wiki_seed, "seed_pin_article_from_wikipedia", wraps=wiki_seed.seed_pin_article_from_wikipedia
            ) as pin_seed,
            mock.patch("urbanlens.dashboard.services.locations.external_links.add_pin_link") as pin_link,
            CaptureQueriesContext(connection) as queries,
            self.captureOnCommitCallbacks(execute=True),
        ):
            LocationCache.set(location, "wikipedia", MATCH, query_key="Hudson River State Hospital")
        sent.assert_not_called()
        return len(queries.captured_queries), pin_seed, pin_link

    def test_the_hook_costs_the_same_with_one_pin_or_many(self) -> None:
        # The first match a test caches costs two queries fewer than every later one, whatever its pin count.
        self._cache_a_match(self._location_with_pins(1, latitude=43.73266))
        quiet_queries, *_ = self._cache_a_match(self.quiet)
        crowded_queries, *_ = self._cache_a_match(self.crowded)

        self.assertEqual(
            crowded_queries,
            quiet_queries,
            f"{CROWD} pins cost {crowded_queries} queries where one cost {quiet_queries}",
        )

    def test_no_pin_is_seeded_or_linked_by_the_hook(self) -> None:
        _queries, pin_seed, pin_link = self._cache_a_match(self.crowded)

        pin_seed.assert_not_called()
        pin_link.assert_not_called()

    def test_the_hook_makes_no_outbound_request(self) -> None:
        """The wiki's first article reaches the cover step, which is where a fetch would start."""
        with mock.patch.object(wiki_seed, "_store_cover_from_url", wraps=wiki_seed._store_cover_from_url) as cover:
            self._cache_a_match(self.crowded)

        cover.assert_called_once()
        self.assertIsNone(cover.call_args.kwargs["pin"], "the cover step ran for a pin, not the wiki")

    def test_the_wiki_takes_the_article(self) -> None:
        """The half that stops the tests above passing against a hook that seeds nothing at all."""
        self._cache_a_match(self.crowded)

        wiki = Wiki.objects.existing_for_location(self.crowded)
        self.assertIsNotNone(wiki)
        self.assertTrue(wiki_seed.is_untouched_wikipedia_seed(wiki.article))
