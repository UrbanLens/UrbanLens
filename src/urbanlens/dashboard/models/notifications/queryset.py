"""QuerySet and Manager for NotificationLog."""

from __future__ import annotations

from typing import Self

from urbanlens.dashboard.models import abstract


class NotificationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for NotificationLog with convenience filters."""

    def unread(self) -> Self:
        """Return only unread notifications."""
        from urbanlens.dashboard.models.notifications.meta import Status

        return self.filter(status=Status.UNREAD)

    def for_profile(self, profile) -> Self:
        """Return notifications belonging to a specific profile."""
        return self.filter(profile=profile)

    def for_display(self) -> Self:
        """Select every relation the notification templates actually read.

        ``notification_item.html`` decides whether to offer Accept/Decline (a
        shared pin) or the three-way merge choice (a suggested visit) by
        reading the ``pin_share``/``visit_suggestion`` reverse OneToOnes, and
        names the sender via ``source_profile``. Without them selected, each
        rendered row costs two extra queries - and the miss is silent, because
        an absent reverse OneToOne raises ``ObjectDoesNotExist``, which Django
        templates swallow rather than surface.

        Every list that renders that partial should go through this, so the
        relation set stays in one place as the template grows.
        """
        return self.select_related("source_profile", "pin_share", "visit_suggestion")

    def mark_read(self) -> int:
        """Mark all matching notifications as read. Returns updated count."""
        from urbanlens.dashboard.models.notifications.meta import Status

        return self.update(status=Status.READ)


class NotificationManager(abstract.DashboardManager.from_queryset(NotificationQuerySet)):
    """Manager for NotificationLog."""
