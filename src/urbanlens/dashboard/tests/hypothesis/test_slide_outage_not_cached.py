"""A provider that could not ask must not have its silence cached."""

from __future__ import annotations

from collections.abc import Generator
from typing import ClassVar
from unittest import mock

from django.core.cache import cache

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider
from urbanlens.dashboard.services.core.gateway import GatewayRequestError


def _slide(name: str) -> SatelliteSlide:
    return SatelliteSlide(img_src=f"https://x/{name}.png", source=name, date="2025", detail="")


class _Provider(SatelliteViewProvider):
    """Yields what it is told, then optionally fails."""

    service_key: ClassVar[str] = "test_slide_provider"
    paid_service: ClassVar[bool] = False

    def __init__(self, slides: list[SatelliteSlide], *, fail: bool) -> None:
        super().__init__()
        self._slides = slides
        self._fail = fail
        self.calls = 0

    def _generate_satellite_slides(
        self, latitude, longitude, *, zoom=17, width=640, height=400, limit=-1
    ) -> Generator[SatelliteSlide]:
        self.calls += 1
        yield from self._slides
        if self._fail:
            raise GatewayRequestError("source unreachable")


class SlideOutageCachingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_a_healthy_empty_answer_is_cached(self) -> None:
        """ "Nothing here" is a real answer and must not be re-fetched forever."""
        provider = _Provider([], fail=False)

        provider.get_satellite_slides(41.7, -73.9)
        provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(provider.calls, 1)

    def test_an_outage_is_not_cached(self) -> None:
        provider = _Provider([], fail=True)

        provider.get_satellite_slides(41.7, -73.9)
        provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(provider.calls, 2, "a cached outage keeps the carousel empty long after it ends")

    def test_slides_yielded_before_the_failure_are_still_returned(self) -> None:
        """A partial answer is worth showing, just not worth remembering."""
        provider = _Provider([_slide("a")], fail=True)

        slides = provider.get_satellite_slides(41.7, -73.9).slides

        self.assertEqual([slide.source for slide in slides], ["a"])

    def test_a_partial_answer_is_not_cached_either(self) -> None:
        provider = _Provider([_slide("a")], fail=True)

        provider.get_satellite_slides(41.7, -73.9)
        provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(provider.calls, 2)

    def test_a_healthy_result_is_cached(self) -> None:
        provider = _Provider([_slide("a")], fail=False)

        first, from_cache_first, _ = provider.get_satellite_slides(41.7, -73.9)
        second, from_cache_second, _ = provider.get_satellite_slides(41.7, -73.9)

        self.assertEqual(provider.calls, 1)
        self.assertFalse(from_cache_first)
        self.assertTrue(from_cache_second)
        self.assertEqual(len(second), len(first))


class DegradationReachesTheCallerTests(TestCase):
    """The provider-level cache skip was only half the rule.

    A two-minute outage emptied the carousel for the rest of the day."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_a_degraded_provider_says_so(self) -> None:
        provider = _Provider([_slide("a")], fail=True)

        fetched = provider.get_satellite_slides(41.7, -73.9)

        self.assertTrue(fetched.degraded, "the flag the panel reads to decide how long to trust an empty carousel")

    def test_a_healthy_provider_does_not(self) -> None:
        provider = _Provider([_slide("a")], fail=False)

        fetched = provider.get_satellite_slides(41.7, -73.9)

        self.assertFalse(fetched.degraded)

    def test_the_carousel_marks_a_degraded_provider_as_not_ok(self) -> None:
        """`ProviderFetchResult.ok` is what SlidesPanelSource.fetch reads."""
        from urbanlens.dashboard.services.pins import external_data

        with mock.patch.object(external_data, "_satellite_gateways", return_value=[_Provider([], fail=True)]):
            _slides, results = external_data.collect_satellite_slides(41.7, -73.9)

        self.assertEqual([result.ok for result in results], [False])
