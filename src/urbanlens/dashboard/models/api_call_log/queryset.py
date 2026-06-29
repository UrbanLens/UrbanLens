"""QuerySet and Manager for ApiCallLog."""

from __future__ import annotations

from datetime import timedelta
from typing import Self

from django.db.models import Avg, Count, Q
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class ApiCallLogQuerySet(abstract.QuerySet):
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

    def successful(self) -> Self:
        """Filter to successful calls."""
        return self.filter(success=True)

    def rate_limited(self) -> Self:
        """Filter to calls that were blocked by rate limiting."""
        return self.filter(was_rate_limited=True)

    def geo_filtered(self) -> Self:
        """Filter to calls that were skipped due to geo filtering."""
        return self.filter(was_geo_filtered=True)

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
            )
            .order_by("service"),
        )


class ApiCallLogManager(abstract.Manager.from_queryset(ApiCallLogQuerySet)):
    """Manager for ApiCallLog that proxies all ApiCallLogQuerySet methods."""
