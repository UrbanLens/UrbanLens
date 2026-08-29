"""``ApiCallLog`` retention must outlast every window that reads the table.

The table has two readers with very different horizons, and pruning is set by
the longer one:

- ``rate_limiter.check_rate_limit`` counts the last 30 days to enforce
  ``calls_per_30_days``;
- ``cost_tracking.monthly_cost_series`` reconstructs a rolling 12-calendar-month
  API-spend chart from the same rows.

Both fail *silently* if retention drops below their window, and in opposite,
easy-to-miss ways: the rate limiter would under-count and let a service exceed
its configured ceiling, while the chart would simply render zeros for months
whose rows had been deleted. Neither raises, and neither is obviously wrong on
screen.

``prune_api_call_logs``' docstring already warns that the model helper's own
90-day default would "silently zero out three-quarters of that chart" - this
turns that warning into something that fails a test run instead of relying on
the next person reading the docstring.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.tasks import _API_CALL_LOG_RETENTION_DAYS

#: The window ``check_rate_limit`` counts over for ``calls_per_30_days``.
_RATE_LIMIT_WINDOW_DAYS = 30

#: Trailing calendar months ``monthly_cost_series`` defaults to.
_COST_SERIES_MONTHS = 12


def _worst_case_cost_series_reach_days(months: int = _COST_SERIES_MONTHS) -> int:
    """Most days back the cost series can ever need, over any starting date.

    The series covers ``months`` trailing *calendar* months, so the oldest row
    it reads is the 1st of the month ``months - 1`` back - and "now" can sit at
    the end of the current month. Calendar months are uneven, so this measures
    the true worst case across a full cycle rather than assuming 31-day months
    (which overstates it by enough to flag a retention that is actually fine).
    """
    import calendar
    import datetime

    worst = 0
    for year in (2023, 2024):  # includes a leap year
        for month in range(1, 13):
            as_of = datetime.date(year, month, calendar.monthrange(year, month)[1])
            oldest_year, oldest_month = divmod((year * 12 + month - 1) - (months - 1), 12)
            oldest = datetime.date(oldest_year, oldest_month + 1, 1)
            worst = max(worst, (as_of - oldest).days)
    return worst


_COST_SERIES_DAYS = _worst_case_cost_series_reach_days()


class ApiCallLogRetentionTests(SimpleTestCase):
    def test_retention_covers_the_rate_limit_window(self) -> None:
        self.assertGreater(
            _API_CALL_LOG_RETENTION_DAYS,
            _RATE_LIMIT_WINDOW_DAYS,
            "pruning inside the rate-limit window makes calls_per_30_days under-count, silently allowing more calls than configured",
        )

    def test_retention_covers_the_cost_chart_window(self) -> None:
        self.assertGreaterEqual(
            _API_CALL_LOG_RETENTION_DAYS,
            _COST_SERIES_DAYS,
            "pruning inside the cost series window silently renders zeros for months whose rows were deleted",
        )

    def test_the_cost_series_default_has_not_moved(self) -> None:
        """The bound above is derived from this default, so a change to it must land here."""
        import inspect

        from urbanlens.dashboard.services.admin import cost_tracking

        default = inspect.signature(cost_tracking.monthly_cost_series).parameters["months"].default

        self.assertEqual(default, _COST_SERIES_MONTHS, "monthly_cost_series' month count changed - re-derive the retention bound above")

    def test_the_rate_limiter_still_uses_a_30_day_window(self) -> None:
        """Likewise: if the limiter's longest window grows, retention has to be rechecked."""
        import inspect

        from urbanlens.dashboard.services.core import rate_limiter

        source = inspect.getsource(rate_limiter.check_rate_limit)

        self.assertIn(
            f"timedelta(days={_RATE_LIMIT_WINDOW_DAYS})",
            source,
            "check_rate_limit's longest window is no longer 30 days - re-derive the retention bound above",
        )


class PruneApiCallLogsUsesTheConfiguredRetentionTests(TestCase):
    """The tests above only check that ``_API_CALL_LOG_RETENTION_DAYS`` is big enough.

    Nothing stops ``prune_api_call_logs`` itself from drifting away from that
    constant - e.g. a future edit that calls ``ApiCallLog.prune_older_than_days()``
    with no argument, silently reverting to the model helper's own 90-day default.
    That is exactly the silent failure this module's docstring warns about, so it
    needs a test that runs the real task against real rows on both sides of the
    boundary, not just a comparison of two numbers.
    """

    def test_prunes_by_the_configured_retention_window(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from urbanlens.dashboard.models.api_call_log import ApiCallLog
        from urbanlens.dashboard.tasks import prune_api_call_logs

        survivor = ApiCallLog.objects.create(service="retention-boundary-test")
        ApiCallLog.objects.filter(pk=survivor.pk).update(created=timezone.now() - timedelta(days=_API_CALL_LOG_RETENTION_DAYS - 1))

        victim = ApiCallLog.objects.create(service="retention-boundary-test")
        ApiCallLog.objects.filter(pk=victim.pk).update(created=timezone.now() - timedelta(days=_API_CALL_LOG_RETENTION_DAYS + 1))

        deleted = prune_api_call_logs()

        self.assertEqual(deleted, 1, "expected exactly the older-than-retention row to be pruned")
        self.assertTrue(ApiCallLog.objects.filter(pk=survivor.pk).exists(), "a row still inside the retention window must survive pruning")
        self.assertFalse(ApiCallLog.objects.filter(pk=victim.pk).exists(), "a row older than the retention window must be pruned")
