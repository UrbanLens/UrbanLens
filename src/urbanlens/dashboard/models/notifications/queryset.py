"""QuerySet and Manager for NotificationLog."""
from __future__ import annotations

from urbanlens.dashboard.models import abstract


class QuerySet(abstract.QuerySet):
    """QuerySet for NotificationLog with convenience filters."""

    def unread(self) -> QuerySet:
        """Return only unread notifications."""
        from urbanlens.dashboard.models.notifications.meta import Status
        return self.filter(status=Status.UNREAD)

    def for_profile(self, profile) -> QuerySet:
        """Return notifications belonging to a specific profile."""
        return self.filter(profile=profile)

    def mark_read(self) -> int:
        """Mark all matching notifications as read. Returns updated count."""
        from urbanlens.dashboard.models.notifications.meta import Status
        return self.update(status=Status.READ)


class Manager(abstract.Manager.from_queryset(QuerySet)):
    """Manager for NotificationLog."""
