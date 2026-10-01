"""QuerySet and Manager for ApiRateLimit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.abstract.queryset import DashboardManager, DashboardQuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit  # noqa: F401 - mypy needs these; ruff does not


class ApiRateLimitQuerySet(DashboardQuerySet["ApiRateLimit"]):
    """QuerySet for ApiRateLimit."""


_ApiRateLimitManagerBase = DashboardManager.from_queryset(ApiRateLimitQuerySet)


class ApiRateLimitManager(_ApiRateLimitManagerBase):
    """Manager for ApiRateLimit."""
