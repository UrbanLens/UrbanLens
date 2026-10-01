"""A panel fetch that returns without landing anything is a failure, not a reason to fetch again.

A source that hits an outage leaves its store empty on purpose, so the outage is not cached as "nothing here". It
used to return normally, which released the single-flight marker without a skip key, so the page's next poll (every
2s, 30 times per viewer) dispatched the same upstream call again. On 2026-09-23 the Web Images panel sent REData
the same failing search/web query dozens of times per page this way.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError
from urbanlens.dashboard.services.pins.external_data import (
    FAILURE_SKIP_TTL_SECONDS,
    get_panel_source,
    run_panel_fetch,
    schedule_panel_fetch,
)

_SOURCE_KEY = "photon"


class QuietFailureTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude=41.73, longitude=-73.92)
        )
        self.source = get_panel_source(_SOURCE_KEY)

    def _dispatches(self) -> mock.MagicMock:
        patcher = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value=object())
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_a_fetch_that_lands_nothing_is_not_dispatched_again_on_the_next_poll(self) -> None:
        enqueue = self._dispatches()
        with mock.patch.object(type(self.source), "fetch", return_value=None):
            run_panel_fetch(_SOURCE_KEY, self.pin, None)

        self.assertFalse(schedule_panel_fetch(_SOURCE_KEY, self.pin))
        enqueue.assert_not_called()

    def test_it_is_suppressed_for_the_failure_window(self) -> None:
        with (
            mock.patch.object(type(self.source), "fetch", return_value=None),
            mock.patch("urbanlens.dashboard.services.pins.external_data.cache.set", wraps=cache.set) as setter,
        ):
            run_panel_fetch(_SOURCE_KEY, self.pin, None)

        setter.assert_any_call(self.source.skip_key(self.pin), 1, FAILURE_SKIP_TTL_SECONDS)

    def test_a_fetch_that_lands_its_data_is_not_suppressed(self) -> None:
        """Anchors the first test: a real answer must leave the source schedulable."""

        def land(pin: Pin) -> None:
            LocationCache.set(pin.location, self.source.cache_source, {"locality": "Poughkeepsie"}, query_key="x")

        with mock.patch.object(type(self.source), "fetch", side_effect=land):
            run_panel_fetch(_SOURCE_KEY, self.pin, None)

        self.assertIsNone(cache.get(self.source.skip_key(self.pin)))

    def test_a_throttled_upstream_is_suppressed_for_as_long_as_it_asked(self) -> None:
        """Not the half hour a disabled service gets: the breaker says exactly when REData reopens."""
        throttled = UpstreamThrottledError("redata_places", retry_after=120)
        with (
            mock.patch.object(type(self.source), "fetch", side_effect=throttled),
            mock.patch("urbanlens.dashboard.services.pins.external_data.cache.set", wraps=cache.set) as setter,
        ):
            run_panel_fetch(_SOURCE_KEY, self.pin, None)

        setter.assert_any_call(self.source.skip_key(self.pin), 1, 120)
