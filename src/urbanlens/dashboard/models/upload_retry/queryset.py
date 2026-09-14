"""QuerySet/Manager for uploads waiting for storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime


class UploadRetryQuerySet(abstract.DashboardQuerySet):
    """QuerySet for :class:`~urbanlens.dashboard.models.upload_retry.model.UploadRetry`."""

    def due(self, now: datetime) -> UploadRetryQuerySet:
        """Restrict to uploads whose next attempt is due, the longest overdue first.

        Args:
            now: The current time.

        Returns:
            This queryset filtered and ordered.
        """
        return self.filter(next_attempt_at__lte=now).order_by("next_attempt_at")


class UploadRetryManager(abstract.DashboardManager.from_queryset(UploadRetryQuerySet)):
    """Manager for :class:`~urbanlens.dashboard.models.upload_retry.model.UploadRetry`."""
