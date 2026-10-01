"""QuerySet/Manager for uploads waiting for storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime

    from urbanlens.dashboard.models.upload_retry.model import UploadRetry  # noqa: F401 - mypy needs these; ruff does not


class UploadRetryQuerySet(abstract.DashboardQuerySet["UploadRetry"]):
    """QuerySet for :class:`~urbanlens.dashboard.models.upload_retry.model.UploadRetry`."""

    def due(self, now: datetime) -> UploadRetryQuerySet:
        """Restrict to uploads whose next attempt is due, the longest overdue first.

        Args:
            now: The current time.

        Returns:
            This queryset filtered and ordered.
        """
        return self.filter(next_attempt_at__lte=now).order_by("next_attempt_at")


_UploadRetryManagerBase = abstract.DashboardManager.from_queryset(UploadRetryQuerySet)


class UploadRetryManager(_UploadRetryManagerBase):
    """Manager for :class:`~urbanlens.dashboard.models.upload_retry.model.UploadRetry`."""
