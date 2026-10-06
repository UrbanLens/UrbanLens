"""QuerySet and Manager for ApiCallLog."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Self

from django.db.models import F, Q, Sum
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Mapping

    from urbanlens.dashboard.models.api_call_log.model import ApiCallLog  # noqa: F401 - mypy needs these; ruff does not


class ApiCallLogQuerySet(abstract.DashboardQuerySet["ApiCallLog"]):
    """QuerySet for ApiCallLog."""

    def for_service(self, service: str) -> Self:
        """Filter to calls for a specific service."""
        return self.filter(service=service)

    def since(self, delta: timedelta) -> Self:
        """Filter to calls made within the last ``delta``."""
        return self.filter(created__gte=timezone.now() - delta)

    def today(self) -> Self:
        """Filter to calls made today (UTC calendar day).

        A half-open range over the stored column rather than ``created__date``.
        The two ask the same question - TIME_ZONE is UTC, so the extraction
        compared against these same instants - but a date extracted from the
        column is a function of it, which ``idxdb_apilog_svc_cdt`` cannot
        answer. This runs inside ``check_rate_limit`` on every outbound call,
        against an append-only log kept for 400 days, so the scan it degraded
        into grew with the whole site's history.

        Returns:
            Calls whose ``created`` falls in today's UTC day.
        """
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.filter(created__gte=start, created__lt=start + timedelta(days=1))

    def this_month(self) -> Self:
        """Filter to calls made in the last 30 days."""
        return self.since(timedelta(days=30))

    def this_calendar_month(self) -> Self:
        """Filter to calls made since the 1st of this UTC month, the window a vendor's free tier resets on.

        Returns:
            Calls whose ``created`` falls in the current UTC calendar month.
        """
        return self.filter(created__gte=timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0))

    def billable(self) -> Self:
        """Filter to calls that actually consumed the service's quota.
        Excludes the entries written for calls that were *skipped* - geo-filtered, rate-limited, service-disabled, and refused for an input that could not return data.
        Those rows exist so a skipped attempt is visible in usage reporting, not because a request went out; counting them against a limit lets a burst of rejections spend a budget no request ever used.

        Returns:
            Filtered queryset.
        """
        return self.filter(was_geo_filtered=False, was_rate_limited=False, was_service_disabled=False, was_rejected_input=False)

    def rejected_input(self) -> Self:
        """Filter to calls refused because their input could not return data."""
        return self.filter(was_rejected_input=True)

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
        rows = self.since(window).exclude(profile__isnull=True).values("profile_id").annotate(spent=Sum("calls")).order_by("-spent", "profile_id")
        return [(row["profile_id"], row["spent"]) for row in rows]

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

    def summary_by_service(self) -> list[Mapping[str, Any]]:
        """Return per-service usage summary for the last 30 days.

        Counted in calls rather than rows, since a tallied service's row stands for a minute of them, and the mean
        response time is weighted the same way.
        """
        timed = Q(response_ms__isnull=False)
        rows = (
            self.this_month()
            .values("service")
            .annotate(
                total=Sum("calls"),
                blocked=Sum("calls", filter=Q(was_rate_limited=True), default=0),
                geo_skipped=Sum("calls", filter=Q(was_geo_filtered=True), default=0),
                rejected_inputs=Sum("calls", filter=Q(was_rejected_input=True), default=0),
                errors=Sum("calls", filter=Q(success=False, was_rate_limited=False, was_geo_filtered=False, was_service_disabled=False, was_rejected_input=False), default=0),
                timed_ms_total=Sum(F("response_ms") * F("calls"), filter=timed),
                timed_calls=Sum("calls", filter=timed),
                total_cost=Sum("cost_estimate"),
            )
            .order_by("service")
        )
        summaries: list[Mapping[str, Any]] = []
        for row in rows:
            timed_ms, timed_calls = row.pop("timed_ms_total"), row.pop("timed_calls")
            summaries.append({**row, "avg_response_ms": timed_ms / timed_calls if timed_calls else None})
        return summaries


_ApiCallLogManagerBase = abstract.DashboardManager.from_queryset(ApiCallLogQuerySet)


class ApiCallLogManager(_ApiCallLogManagerBase):
    """Manager for ApiCallLog that proxies all ApiCallLogQuerySet methods."""
