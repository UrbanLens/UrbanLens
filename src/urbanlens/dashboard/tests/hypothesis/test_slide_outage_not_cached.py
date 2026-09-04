"""A provider that could not ask must not have its silence cached.

`get_satellite_slides`/`get_street_view_slides` cache what a provider yields.
That is right for "this place has no imagery" and wrong for "we could not
reach the source": the second is transient, but a cached empty list outlives it
and nothing retries, so the carousel stays empty long after the outage ends.
This is the same defect class as caching a failed panel fetch, which
bin/check_outage_not_cached.py exists to prevent - that check only inspects
functions named `fetch`, which is why this one went unnoticed.

Providers signal the difference by letting their gateway error propagate out of
the generator instead of swallowing it. Slides yielded before the failure are
kept: a partial answer is worth showing, it just is not worth remembering.
"""

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

    `_collect_slides` correctly refused to cache a partial answer, but
    `get_satellite_slides` then returned `(slides, False)` and dropped the
    `degraded` flag - so `collect_satellite_slides` recorded `ok=True`, the
    panel saw `complete=True`, and stored its readiness marker for twelve hours
    instead of five minutes. A two-minute outage emptied the carousel for the
    rest of the day.
    """

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
