"""An outage must not be cached as "there is nothing here"."""

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
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            side_effect=LocationContextUnavailableError("source_error", "503"),
        ):
            self._source().fetch(self.pin)

        self.assertEqual(
            self._cached(), 0, "caching the outage makes it permanent - nothing refetches a source that has a row"
        )

    def test_a_genuine_empty_result_is_cached(self) -> None:
        """Asked and told nothing is a real answer, and must not be refetched forever."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            return_value=[],
        ):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_results_are_cached(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            return_value=[{"url": "https://example.test/a.jpg"}],
        ):
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
        with (
            mock.patch(targets[0], side_effect=LocationContextUnavailableError("source_error", "down")),
            mock.patch(targets[1], side_effect=LocationContextUnavailableError("source_error", "down")),
            mock.patch(targets[2], side_effect=LocationContextUnavailableError("source_error", "down")),
        ):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 0)


class RedataPartialProviderOutageTests(TestCase):
    """A provider outage *inside* a successful request is still an outage.

    The tests above guard a failed request."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.73, longitude=-73.92, official_name="Hudson River State Hospital")
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name="HRSH")

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.redata_permits import BuildingPermitsPanelSource

        return BuildingPermitsPanelSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def _envelope(self, *, complete: bool, results: list[dict]):
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

        return LocationContextEnvelope(count=len(results), complete=complete, results=results, providers=[])

    def test_an_empty_incomplete_answer_is_not_cached(self) -> None:
        source = self._source()
        with mock.patch.object(type(source), "fetch_envelope", return_value=self._envelope(complete=False, results=[])):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 0, "could not ask is not an answer - a row here makes the blank permanent")

    def test_an_empty_complete_answer_is_cached(self) -> None:
        """Asked and told nothing is a real result, and must not be refetched forever."""
        source = self._source()
        with mock.patch.object(type(source), "fetch_envelope", return_value=self._envelope(complete=True, results=[])):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_a_partial_answer_with_rows_is_cached(self) -> None:
        """One flaky provider must not stop the other four's rows being stored."""
        source = self._source()
        with mock.patch.object(
            type(source),
            "fetch_envelope",
            return_value=self._envelope(complete=False, results=[{"permit_number": "A-1"}]),
        ):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_the_rule_is_inherited_by_every_panel_built_on_the_base(self) -> None:
        """Stated as a property of the base rather than of one panel.

        Six panels share this fetch; asserting it on one of them only proves
        the base works, which is the point of moving the rule there.
        """
        from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

        for source_cls in RedataInfoPanelSource.__subclasses__():
            with self.subTest(panel=source_cls.__name__):
                self.assertIs(
                    source_cls.fetch,
                    RedataInfoPanelSource.fetch,
                    f"{source_cls.__name__} overrides fetch and so opts out of the outage rule",
                )
                self.assertTrue(
                    getattr(source_cls, "payload_key", ""),
                    f"{source_cls.__name__} declares no payload_key, so the inherited fetch has nowhere to write",
                )
