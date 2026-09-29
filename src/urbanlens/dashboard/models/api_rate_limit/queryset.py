"""QuerySet and Manager for ApiRateLimit."""

from __future__ import annotations

from urbanlens.dashboard.models.abstract.queryset import DashboardManager, DashboardQuerySet


class ApiRateLimitQuerySet(DashboardQuerySet):
    """QuerySet for ApiRateLimit."""


class ApiRateLimitManager(DashboardManager):
    """Manager for ApiRateLimit."""

    def get_queryset(self) -> ApiRateLimitQuerySet:
        """Return an ApiRateLimitQuerySet."""
        return ApiRateLimitQuerySet(self.model, using=self._db)
