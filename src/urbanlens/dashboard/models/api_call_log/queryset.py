"""QuerySet and Manager for ApiCallLog."""

from __future__ import annotations

from datetime import timedelta
from typing import Self

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class ApiCallLogQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ApiCallLog."""

    def for_service(self, service: str) -> Self:
        """Filter to calls for a specific service."""
        return self.filter(service=service)

    def since(self, delta: timedelta) -> Self:
        """Filter to calls made within the last ``delta``."""
        return self.filter(created__gte=timezone.now() - delta)

    def today(self) -> Self:
        """Filter to calls made today (UTC calendar day)."""
        return self.filter(created__date=timezone.now().date())

    def this_week(self) -> Self:
        """Filter to calls made in the last 7 days."""
        return self.since(timedelta(days=7))

    def this_month(self) -> Self:
        """Filter to calls made in the last 30 days."""
        return self.since(timedelta(days=30))

    def billable(self) -> Self:
        """Filter to calls that actually consumed the service's quota.

        Excludes the three kinds of entry the limiter writes for calls it
        *skipped* - geo-filtered, rate-limited, and service-disabled. Those rows
        exist so a skipped attempt is visible in usage reporting, not because a
        request went out; counting them against a limit lets a burst of
        rejections spend a budget no request ever used.

        A call that went out and failed is still billable - the remote service
        counted it.

        Returns:
            Filtered queryset.
        """
        return self.filter(was_geo_filtered=False, was_rate_limited=False, was_service_disabled=False)

    def successful(self) -> Self:
        """Filter to successful calls."""
        return self.filter(success=True)

    def rate_limited(self) -> Self:
        """Filter to calls that were blocked by rate limiting."""
        return self.filter(was_rate_limited=True)

    def geo_filtered(self) -> Self:
        """Filter to calls that were skipped due to geo filtering."""
        return self.filter(was_geo_filtered=True)

    def service_disabled(self) -> Self:
        """Filter to calls that were skipped due to service being disabled."""
        return self.filter(was_service_disabled=True)

    def summary_by_service(self) -> list[dict]:
        """Return per-service usage summary for the last 30 days."""
        return list(
            self.this_month()
            .values("service")
            .annotate(
                total=Count("id"),
                blocked=Count("id", filter=Q(was_rate_limited=True)),
                geo_skipped=Count("id", filter=Q(was_geo_filtered=True)),
                errors=Count("id", filter=Q(success=False, was_rate_limited=False, was_geo_filtered=False)),
                avg_response_ms=Avg("response_ms"),
                total_cost=Sum("cost_estimate"),
            )
            .order_by("service"),
        )


class ApiCallLogManager(abstract.DashboardManager.from_queryset(ApiCallLogQuerySet)):
    """Manager for ApiCallLog that proxies all ApiCallLogQuerySet methods."""
