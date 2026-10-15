"""``ApiCallLog`` retention must outlast every window that reads the table."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.tasks import _API_CALL_LOG_RETENTION_DAYS

#: The window ``check_rate_limit`` counts over for ``calls_per_30_days``.
_RATE_LIMIT_WINDOW_DAYS = 30


class ApiCallLogRetentionTests(SimpleTestCase):
    def test_retention_covers_the_rate_limit_window(self) -> None:
        self.assertGreater(
            _API_CALL_LOG_RETENTION_DAYS,
            _RATE_LIMIT_WINDOW_DAYS,
            "pruning inside the rate-limit window makes calls_per_30_days under-count, silently allowing more calls than configured",
        )

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
    """
    The tests above only check that ``_API_CALL_LOG_RETENTION_DAYS`` is big enough.

    Nothing stops ``prune_api_call_logs`` itself from drifting away from that constant - e.g. a future edit that
    calls ``ApiCallLog.prune_older_than_days()`` with no argument, silently reverting to the model helper's own
    90-day default."""

    def test_prunes_by_the_configured_retention_window(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from urbanlens.dashboard.models.api_call_log import ApiCallLog
        from urbanlens.dashboard.tasks import prune_api_call_logs

        survivor = ApiCallLog.objects.create(service="retention-boundary-test")
        ApiCallLog.objects.filter(pk=survivor.pk).update(
            created=timezone.now() - timedelta(days=_API_CALL_LOG_RETENTION_DAYS - 1)
        )

        victim = ApiCallLog.objects.create(service="retention-boundary-test")
        ApiCallLog.objects.filter(pk=victim.pk).update(
            created=timezone.now() - timedelta(days=_API_CALL_LOG_RETENTION_DAYS + 1)
        )

        deleted = prune_api_call_logs()

        self.assertEqual(deleted, 1, "expected exactly the older-than-retention row to be pruned")
        self.assertTrue(
            ApiCallLog.objects.filter(pk=survivor.pk).exists(),
            "a row still inside the retention window must survive pruning",
        )
        self.assertFalse(
            ApiCallLog.objects.filter(pk=victim.pk).exists(), "a row older than the retention window must be pruned"
        )
