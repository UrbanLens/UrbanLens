"""QuerySet and Manager for ApiRateLimit."""

from __future__ import annotations

from urbanlens.dashboard.models.abstract.queryset import Manager, QuerySet


class ApiRateLimitQuerySet(QuerySet):
    """QuerySet for ApiRateLimit."""

    def enabled(self) -> ApiRateLimitQuerySet:
        """Return only enabled rate limit configs."""
        return self.filter(enabled=True)


class ApiRateLimitManager(Manager):
    """Manager for ApiRateLimit."""

    def get_queryset(self) -> ApiRateLimitQuerySet:
        """Return an ApiRateLimitQuerySet."""
        return ApiRateLimitQuerySet(self.model, using=self._db)
