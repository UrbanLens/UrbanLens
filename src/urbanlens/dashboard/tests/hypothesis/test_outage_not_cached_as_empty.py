"""An outage must not be cached as "there is nothing here".

The existence of a ``LocationCache`` row is what marks a source as having run
(see ``LocationCacheEnrichmentSource.missing_filter``), so writing an empty
result after a failed fetch turns a transient outage into a permanent gap that
nothing retries.

This is not hypothetical. The SearXNG instance behind image search returned
403s for a period; every pin whose media was fetched in that window cached an
empty list, and stayed empty afterwards - the emptiness outlived the outage.
The same shape existed in the site-conditions panel.

The distinction the code has to keep: *asked and told nothing* is a result
worth caching; *could not ask* is not.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError


class SearxngImageOutageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.73, longitude=-73.92, official_name="Hudson River State Hospital")
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name="HRSH")

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.searxng_images import SearxngImageMediaSource

        return SearxngImageMediaSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def test_an_outage_leaves_the_source_unfetched(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web", side_effect=LocationContextUnavailableError("source_error", "503")):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 0, "caching the outage makes it permanent - nothing refetches a source that has a row")

    def test_a_genuine_empty_result_is_cached(self) -> None:
        """Asked and told nothing is a real answer, and must not be refetched forever."""
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web", return_value=[]):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_results_are_cached(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web", return_value=[{"url": "https://example.test/a.jpg"}]):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)


class SiteConditionsOutageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.74, longitude=-73.93)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.redata_site_conditions import SiteConditionsPanelSource

        return SiteConditionsPanelSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def test_a_total_outage_leaves_it_unfetched(self) -> None:
        targets = [
            "urbanlens.dashboard.services.apis.locations.redata_land_cover_gateway.RedataLandCoverGateway.get_land_cover",
            "urbanlens.dashboard.services.apis.locations.redata_walkability_gateway.RedataWalkabilityGateway.get_walkability",
            "urbanlens.dashboard.services.apis.locations.redata_soil_gateway.RedataSoilGateway.get_soil_components",
        ]
        with mock.patch(targets[0], side_effect=LocationContextUnavailableError("source_error", "down")), \
             mock.patch(targets[1], side_effect=LocationContextUnavailableError("source_error", "down")), \
             mock.patch(targets[2], side_effect=LocationContextUnavailableError("source_error", "down")):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 0)
