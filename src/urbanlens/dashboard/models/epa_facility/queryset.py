"""QuerySet and manager for EpaFacility."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class EpaFacilityQuerySet(abstract.DashboardQuerySet):
    """Query helpers for persisted EPA ECHO facility records."""

    def with_detail(self):
        """Rows that have a fetched Detailed Facility Report (real coordinates known)."""
        return self.filter(detail_fetched_at__isnull=False)


class EpaFacilityManager(abstract.DashboardManager.from_queryset(EpaFacilityQuerySet)):
    """Manager for EpaFacility."""
