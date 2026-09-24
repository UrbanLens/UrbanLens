"""QuerySet/Manager for refused Celery enqueues."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from datetime import datetime


class TaskOutboxEntryQuerySet(abstract.DashboardQuerySet):
    """QuerySet for :class:`~urbanlens.dashboard.models.task_outbox.model.TaskOutboxEntry`."""

    def due(self, now: datetime) -> TaskOutboxEntryQuerySet:
        """Restrict to entries whose next attempt is due, the longest overdue first.

        Args:
            now: The current time.

        Returns:
            This queryset filtered and ordered.
        """
        return self.filter(next_attempt_at__lte=now).order_by("next_attempt_at", "pk")


class TaskOutboxEntryManager(abstract.DashboardManager.from_queryset(TaskOutboxEntryQuerySet)):
    """Manager for :class:`~urbanlens.dashboard.models.task_outbox.model.TaskOutboxEntry`."""
