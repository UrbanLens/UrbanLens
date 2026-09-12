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

    def service_disabled(self) -> Self:
        """Filter to calls that were skipped due to service being disabled."""
        return self.filter(was_service_disabled=True)

    def for_profile(self, profile) -> Self:
        """Filter to calls made on one profile's behalf."""
        return self.filter(profile=profile)

    def usage_by_profile(self, window: timedelta) -> list[tuple[int, int]]:
        """Who consumed this queryset's calls over ``window``, heaviest first.

        Composable rather than self-filtering: chain ``for_service`` and
        ``billable`` to ask about quota actually spent, or leave them off to
        include the refusals, which are the record of demand that went unmet.

        Unattributed rows are excluded rather than grouped under a null key -
        the site's own scheduled work is not a user's consumption, and a
        fair-share decision that counted it would restrain people for it.

        Args:
            window: How far back to look.

        Returns:
            ``(profile_id, calls)`` pairs, heaviest first, ties by profile id.
        """
        rows = self.since(window).exclude(profile__isnull=True).values("profile_id").annotate(calls=Count("id")).order_by("-calls", "profile_id")
        return [(row["profile_id"], row["calls"]) for row in rows]

    def active_consumers(self, window: timedelta) -> int:
        """How many distinct people used this queryset's service over ``window``.

        The denominator of a fair share: one active consumer may reasonably
        have the whole budget, a hundred may not.

        Args:
            window: How far back to look.

        Returns:
            Count of distinct attributed profiles, ignoring unattributed rows.
        """
        return self.since(window).exclude(profile__isnull=True).values("profile_id").distinct().count()

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
