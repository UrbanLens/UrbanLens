"""QuerySet and Manager for NotificationLog."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class NotificationQuerySet(abstract.QuerySet):
    """QuerySet for NotificationLog with convenience filters."""

    def unread(self) -> Self:
        """Return only unread notifications."""
        from urbanlens.dashboard.models.notifications.meta import Status

        return self.filter(status=Status.UNREAD)

    def for_profile(self, profile) -> Self:
        """Return notifications belonging to a specific profile."""
        return self.filter(profile=profile)

    def mark_read(self) -> int:
        """Mark all matching notifications as read. Returns updated count."""
        from urbanlens.dashboard.models.notifications.meta import Status

        return self.update(status=Status.READ)


class NotificationManager(abstract.Manager.from_queryset(NotificationQuerySet)):
    """Manager for NotificationLog."""
