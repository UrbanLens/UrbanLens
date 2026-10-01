"""P122: a refused call must be catchable everywhere a failed one already is.

`rate_limiter._reserve_call` refuses a call by raising `RequestCancelledError` (or one of its three
subclasses) - the family every ordinary gateway call passes through via `_RateLimitedSession`. That
family subclassed only `DashboardError`, not `GatewayRequestError`, so a view or service that
degrades gracefully on `except GatewayRequestError` - the contract every other gateway failure
raises - let a refusal escape as an unhandled 500 instead. Refusals are routine: development and the
demo refuse most services outright, and R29 fires `RateLimiterUnavailableError` whenever `ul_web` is
at its connection limit.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.exceptions import DashboardError
from urbanlens.dashboard.services.core.gateway import (
    UPSTREAM_BUSY_DEFAULT_SECONDS,
    GatewayRateLimitedError,
    GatewayRequestError,
    UpstreamBusyError,
)
from urbanlens.dashboard.services.core.rate_limiter import (
    RateLimiterUnavailableError,
    RateLimitExceededError,
    RequestCancelledError,
    ServiceDisabledError,
    UpstreamThrottledError,
)


class EveryRefusalIsAGatewayRequestErrorTests(SimpleTestCase):
    """The fix: every existing `except GatewayRequestError` now also catches a refusal."""

    def test_request_cancelled_error_is_a_gateway_request_error(self) -> None:
        self.assertIsInstance(RequestCancelledError("some_service"), GatewayRequestError)

    def test_rate_limit_exceeded_error_is_a_gateway_request_error(self) -> None:
        self.assertIsInstance(RateLimitExceededError("some_service"), GatewayRequestError)

    def test_service_disabled_error_is_a_gateway_request_error(self) -> None:
        self.assertIsInstance(ServiceDisabledError("some_service"), GatewayRequestError)

    def test_rate_limiter_unavailable_error_is_a_gateway_request_error(self) -> None:
        self.assertIsInstance(RateLimiterUnavailableError("some_service"), GatewayRequestError)


class ExistingNarrowCatchesStillWorkTests(SimpleTestCase):
    """The other half of the fix: nothing that already catches the raw type must stop seeing it.

    Several call sites (`external_data.py`, `nominatim.py`, `cid_resolution.py`, and others) catch
    `RequestCancelledError`/`RateLimitExceededError` directly rather than through
    `GatewayRequestError`. Adding a new base class must not change what type actually gets raised.
    """

    def test_request_cancelled_error_is_still_a_dashboard_error(self) -> None:
        self.assertIsInstance(RequestCancelledError("some_service"), DashboardError)

    def test_rate_limit_exceeded_error_is_still_a_request_cancelled_error(self) -> None:
        self.assertIsInstance(RateLimitExceededError("some_service"), RequestCancelledError)

    def test_the_message_and_service_attribute_are_unchanged(self) -> None:
        exc = RateLimitExceededError("overture_maps")
        self.assertEqual(str(exc), "Rate limit exceeded for service 'overture_maps'")
        self.assertEqual(exc.service, "overture_maps")


class UpstreamThrottledErrorTests(SimpleTestCase):
    """The throttled refusal is all four things its callers catch it as, carrying the upstream's wait."""

    def setUp(self) -> None:
        super().setUp()
        self.retry_after = UPSTREAM_BUSY_DEFAULT_SECONDS + 7
        self.exc = UpstreamThrottledError("redata_places", retry_after=self.retry_after)

    def test_it_is_caught_by_every_family_it_belongs_to(self) -> None:
        for family in (
            RateLimitExceededError,
            RequestCancelledError,
            UpstreamBusyError,
            GatewayRateLimitedError,
            GatewayRequestError,
        ):
            with self.subTest(family=family.__name__):
                self.assertIsInstance(self.exc, family)

    def test_it_carries_the_upstreams_wait_not_the_default(self) -> None:
        self.assertEqual(self.exc.retry_after, self.retry_after)

    def test_its_message_and_service_name_the_throttled_upstream(self) -> None:
        message = f"'redata_places' is throttled upstream for another {self.retry_after}s"
        self.assertEqual(str(self.exc), message)
        self.assertEqual(self.exc.args, (message,))
        self.assertEqual(self.exc.service, "redata_places")
