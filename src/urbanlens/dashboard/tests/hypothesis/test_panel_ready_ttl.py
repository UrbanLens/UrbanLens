"""A panel that never got an answer must not be remembered as empty for 12 hours.

``SlidesPanelSource.fetch`` warms every imagery provider and then sets a "ready"
marker. It used to call ``self.collect(...)``, discard the per-provider outcomes
it returns, and set that marker for :data:`SLIDES_READY_TTL_SECONDS` (12 hours)
unconditionally.

Two things made that wrong together. The collectors caught
``RequestCancelledError`` - the base class of ``RateLimitExceededError`` - logged
it at debug and appended *no* result at all, so a rate-limited provider registered
as neither success nor failure. And ``fetch`` threw the results away regardless.
So a provider refused by its own rate limiter left the panel marked warm and empty
for twelve hours, which is indistinguishable to every reader from "this location
genuinely has no imagery".

The distinction now drives the marker's lifetime, mirroring
``spotguessr.geo_bonus``, which gives a real "nothing found" a 30-day TTL and a
failed lookup 60 seconds for exactly this reason. A *disabled* service stays
silent: that is a stable state, not a transient one, and re-warming every few
minutes because an admin turned a provider off would be worse than the bug.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, ServiceDisabledError
from urbanlens.dashboard.services.pins.external_data import (
    FAILURE_SKIP_TTL_SECONDS,
    SLIDES_READY_TTL_SECONDS,
    ProviderFetchResult,
    SatellitePanelSource,
    collect_satellite_slides,
)


class _StubGateway:
    """One imagery provider whose behaviour the test dictates."""

    service_key = "stub_imagery"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get_satellite_slides(self, lat: float, lng: float) -> tuple[list, bool]:
        if self._error is not None:
            raise self._error
        return [], False


class PanelReadyTtlTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=41.35, longitude=-71.45))
        self.source = SatellitePanelSource()
        cache.delete(self.source.ready_key(self.pin))

    def _fetch_with(self, error: Exception | None) -> int:
        """Run a fetch and return the TTL it stamped on the ready marker.

        The marker's lifetime is the thing under test and the test cache backend
        (LocMemCache) cannot report a key's remaining TTL, so the ``cache.set``
        call itself is observed rather than its aftermath.
        """
        return self._fetch_with_gateways([_StubGateway(error)])

    def _fetch_with_gateways(self, gateways: list[_StubGateway]) -> int:
        with (
            mock.patch("urbanlens.dashboard.services.pins.external_data._satellite_gateways", return_value=gateways),
            mock.patch("urbanlens.dashboard.services.pins.external_data.cache.set", wraps=cache.set) as cache_set,
        ):
            self.source.fetch(self.pin)

        marker_calls = [call for call in cache_set.call_args_list if call.args and call.args[0] == self.source.ready_key(self.pin)]
        self.assertEqual(len(marker_calls), 1, "fetch must stamp the ready marker exactly once")
        return marker_calls[0].args[2]

    def test_a_rate_limited_provider_is_recorded_as_a_failure(self) -> None:
        """It used to be swallowed by the RequestCancelledError arm, appending no
        result, so nothing downstream could tell it had happened."""
        with mock.patch(
            "urbanlens.dashboard.services.pins.external_data._satellite_gateways",
            return_value=[_StubGateway(RateLimitExceededError("stub_imagery"))],
        ):
            _, results = collect_satellite_slides(41.35, -71.45)

        self.assertEqual([r.ok for r in results], [False])

    def test_a_genuinely_empty_result_is_trusted_for_the_full_window(self) -> None:
        """A location with no imagery must not be re-queried every few minutes."""
        self.assertEqual(self._fetch_with(None), SLIDES_READY_TTL_SECONDS)
        self.assertTrue(cache.get(self.source.ready_key(self.pin)))

    def test_a_rate_limited_fetch_is_only_trusted_briefly(self) -> None:
        self.assertEqual(self._fetch_with(RateLimitExceededError("stub_imagery")), FAILURE_SKIP_TTL_SECONDS)

    def test_a_failed_provider_is_only_trusted_briefly(self) -> None:
        self.assertEqual(self._fetch_with(RuntimeError("provider exploded")), FAILURE_SKIP_TTL_SECONDS)

    def test_a_disabled_service_still_counts_as_a_settled_answer(self) -> None:
        """An admin turning a provider off is stable, not transient - re-warming
        every few minutes forever would be worse than the bug being fixed."""
        self.assertEqual(self._fetch_with(ServiceDisabledError("stub_imagery")), SLIDES_READY_TTL_SECONDS)

    def test_the_marker_is_always_set(self) -> None:
        """Whatever happened, the panel must not poll in a tight loop."""
        for error in (None, RateLimitExceededError("stub_imagery"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                cache.delete(self.source.ready_key(self.pin))
                self._fetch_with(error)

                self.assertTrue(self.source.is_ready(self.pin))

    def test_a_partial_success_is_not_treated_as_settled(self) -> None:
        """One provider answering does not make the others' silence meaningful."""
        gateways = [_StubGateway(None), _StubGateway(RateLimitExceededError("stub_imagery"))]

        self.assertEqual(self._fetch_with_gateways(gateways), FAILURE_SKIP_TTL_SECONDS)

    def test_the_result_shape_is_what_fetch_reads(self) -> None:
        """Guards the checks above from passing on a renamed/absent ``ok``."""
        self.assertTrue(ProviderFetchResult("svc", from_cache=False, count=0).ok)
        self.assertFalse(ProviderFetchResult("svc", from_cache=False, count=0, ok=False).ok)
