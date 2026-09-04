"""A rejected call must not consume the budget that rejected it.

Every blocked attempt writes an ``ApiCallLog`` row so the rejection is visible in
usage reporting - `_reserve_call` creates one with ``was_rate_limited=True``
before raising, and another with ``was_service_disabled=True`` for a disabled
service. ``check_rate_limit`` then counts rows for the service and excludes only
``was_geo_filtered=True``, so those rejection rows count as if they were real
outbound calls.

The per-minute limit is what makes this bite. A burst that exceeds it produces
one rejection row per over-limit attempt, and every one of those is then charged
against the *daily* and *30-day* budgets, which are far larger and much slower to
recover. A caller that retries into a per-minute wall therefore burns a day's
allowance without a single request leaving the process - and the more aggressively
it retries, the faster its real budget disappears.

That the author excluded ``was_geo_filtered`` is what makes this a bug rather
than a design choice: skipped calls were already understood not to count. Two of
the three skip reasons were missed.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, _reserve_call, check_rate_limit

_SERVICE = "test_budget_service"


class RejectionsAreNotBilledTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        ApiCallLog.objects.filter(service=_SERVICE).delete()
        ApiRateLimit.objects.update_or_create(
            service=_SERVICE,
            defaults={
                "calls_per_minute": 2,
                "calls_per_day": 10,
                "calls_per_30_days": None,
                "min_interval_seconds": None,
                "enabled": True,
            },
        )

    def _attempt(self) -> bool:
        """Try one call; True if it was permitted."""
        try:
            _reserve_call(_SERVICE, endpoint="/x")
        except RateLimitExceededError:
            return False
        return True

    def test_the_minute_limit_itself_still_works(self) -> None:
        """Anchors the rest: without this, a passing budget test could be vacuous."""
        self.assertTrue(self._attempt())
        self.assertTrue(self._attempt())

        self.assertFalse(self._attempt(), "a third call in the same minute should be refused")

    def test_rejected_attempts_do_not_consume_the_daily_budget(self) -> None:
        """The property: only calls that actually went out are charged."""
        for _ in range(8):
            self._attempt()  # 2 permitted, 6 rejected

        permitted = ApiCallLog.objects.filter(service=_SERVICE, was_rate_limited=False).count()
        self.assertEqual(permitted, 2, "sanity: exactly two calls should have been permitted")

        # The daily limit is 10. Only 2 real calls happened, so 8 of the day's
        # allowance must remain - the 6 rejections must not have been charged.
        billed = (
            ApiCallLog.objects.for_service(_SERVICE)
            .today()
            .exclude(was_geo_filtered=True)
            .exclude(was_rate_limited=True)
            .exclude(was_service_disabled=True)
            .count()
        )
        self.assertEqual(
            billed, 2, f"rejections were charged against the daily budget: {billed} billed for 2 real calls"
        )

    def test_the_limiter_agrees_once_the_minute_window_is_irrelevant(self) -> None:
        """A day's budget must not be exhausted by a burst of refusals.

        With the per-minute limit removed, only the daily limit applies. Ten
        rejection rows were already written by the burst above; if those count,
        the service is wrongly out of budget for the rest of the day.
        """
        for _ in range(12):
            self._attempt()  # 2 permitted, 10 rejected

        ApiRateLimit.objects.filter(service=_SERVICE).update(calls_per_minute=None)

        self.assertTrue(
            check_rate_limit(_SERVICE),
            "the day's budget (10) was exhausted by rejections, though only 2 real calls were made",
        )

    def test_a_disabled_services_skipped_rows_are_not_charged_either(self) -> None:
        """Same reasoning, different skip reason."""
        ApiCallLog.objects.create(service=_SERVICE, endpoint="/x", success=False, was_service_disabled=True)
        ApiCallLog.objects.create(service=_SERVICE, endpoint="/x", success=False, was_service_disabled=True)
        ApiRateLimit.objects.filter(service=_SERVICE).update(calls_per_minute=2, calls_per_day=2)

        self.assertTrue(check_rate_limit(_SERVICE), "rows for a disabled service were charged against the limit")

    def test_a_failed_call_that_did_go_out_is_still_charged(self) -> None:
        """The opposite error: a network failure did consume the quota upstream."""
        ApiCallLog.objects.create(service=_SERVICE, endpoint="/x", success=False)
        ApiCallLog.objects.create(service=_SERVICE, endpoint="/x", success=False)
        ApiRateLimit.objects.filter(service=_SERVICE).update(calls_per_minute=2, calls_per_day=2)

        self.assertFalse(check_rate_limit(_SERVICE), "attempted-but-failed calls must still count")

    def test_the_enrichment_budget_is_computed_the_same_way(self) -> None:
        """`compute_service_budget` had the identical count, and the same flaw.

        It decides how many enrichment calls a sweep may still make. Inflating
        "used" with rejection rows shrinks that budget, and a large enough burst
        drives it to zero - stopping enrichment for a service whose real quota is
        mostly unspent.
        """
        from urbanlens.dashboard.services.locations.enrichment import compute_service_budget

        ApiRateLimit.objects.filter(service=_SERVICE).update(calls_per_minute=2, calls_per_day=10)
        for _ in range(12):
            self._attempt()  # 2 permitted, 10 rejected

        budget = compute_service_budget(_SERVICE)

        self.assertIsNotNone(budget)
        self.assertGreater(budget, 0, "rejections consumed the enrichment budget for a service with quota left")
