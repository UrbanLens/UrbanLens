"""The 30-day API spend summary must count every service that actually cost money.

``api_spend_summary_30d`` decided what to include by asking whether a service has
a flat ``ServiceDefaults.cost_per_call``. Of the 46 registered services exactly
one does. Every AI service - assistant, article expansion/safety, the trivia
chain, photo keywords - prices per call from real token usage instead, writes
that onto ``ApiCallLog.cost_estimate``, and declares no flat rate. So the number
shown on the site-admin cost page and the public running-costs page silently
excluded the most expensive services in the app and counted them as "unpriced".

The right question is "did this service record any cost", not "does it have a
flat price".
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.admin.cost_tracking import api_spend_summary_30d
from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults


class ApiSpendSummaryTests(TestCase):
    """Recorded per-call costs must reach the totals."""

    def test_a_service_priced_per_call_is_counted(self) -> None:
        """``ai_photo_keywords`` has no flat rate but logs a real cost per call.

        Checked before *and* after the calls are logged, on the same service: a stub that
        always reports every registered service as priced would pass the second half alone,
        and the original bug (keying "priced" off the flat rate) would pass the first half
        alone but never flip once real cost is recorded.
        """
        baseline = api_spend_summary_30d()
        self.assertEqual(baseline["priced_services"], [], "nothing has recorded a cost yet")
        self.assertIsNone(baseline["total_cost_30d"])

        ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal("0.01"))
        ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal("0.02"))

        summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("0.03"))
        names = [entry["display_name"] for entry in summary["priced_services"]]
        self.assertEqual(len(names), 1, f"expected the service to be counted, got {summary['priced_services']}")

    def test_a_flat_rate_service_is_still_counted(self) -> None:
        ApiCallLog.objects.create(service="google_geocoding", success=True, cost_estimate=Decimal("0.005"))

        summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("0.005"))

    def test_both_kinds_sum_together_and_sort_by_cost(self) -> None:
        ApiCallLog.objects.create(service="google_geocoding", success=True, cost_estimate=Decimal("0.005"))
        ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal("0.50"))

        summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("0.505"))
        costs = [entry["cost_30d"] for entry in summary["priced_services"]]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_a_service_that_recorded_no_cost_stays_unpriced(self) -> None:
        """A free/unpriced service must not start reporting a zero-cost line."""
        ApiCallLog.objects.create(service="wikipedia", success=True, cost_estimate=None)

        summary = api_spend_summary_30d()

        self.assertIsNone(summary["total_cost_30d"])
        self.assertEqual(summary["priced_services"], [])
        # Every registered service is unpriced here (nothing else recorded a cost) - an
        # exact count catches an off-by-one undercount that `assertGreater(..., 0)` would miss.
        self.assertEqual(summary["unpriced_service_count"], len(all_service_defaults()))

    def test_a_recorded_zero_cost_counts_as_priced_not_unpriced(self) -> None:
        """A call that genuinely cost $0.00 recorded a cost - distinct from never recording one.

        ``cost_30d is None`` is the unpriced test in the implementation; a regression to a
        falsy check (``if not cost_30d``) would misclassify this exact case.
        """
        ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal(0))

        summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal(0))
        names = [entry["display_name"] for entry in summary["priced_services"]]
        self.assertEqual(len(names), 1)
        self.assertEqual(summary["unpriced_service_count"], len(all_service_defaults()) - 1)

    def test_calls_older_than_30_days_are_excluded(self) -> None:
        """The trailing-30-day figure must not be inflated by older spend."""
        ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal("0.01"))
        old = ApiCallLog.objects.create(service="ai_photo_keywords", success=True, cost_estimate=Decimal("100.00"))
        ApiCallLog.objects.filter(pk=old.pk).update(created=timezone.now() - timedelta(days=45))

        summary = api_spend_summary_30d()

        self.assertEqual(summary["total_cost_30d"], Decimal("0.01"))
