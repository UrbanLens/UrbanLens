"""QuerySet and manager for SafetyCheckin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class SafetyCheckinQuerySet(abstract.QuerySet):
    """QuerySet for SafetyCheckin records."""

    def due_for_reminder(self) -> Self:
        """Return scheduled check-ins whose expected check-in time has arrived.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        return self.filter(status=SafetyCheckinStatus.SCHEDULED, checkin_by__lte=timezone.now())

    def overdue(self) -> Self:
        """Return check-ins whose grace period has elapsed with no response.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        return self.annotate(
            overdue_at=ExpressionWrapper(F("checkin_by") + F("grace_period"), output_field=DateTimeField()),
        ).filter(status=SafetyCheckinStatus.AWAITING_CHECKIN, overdue_at__lte=timezone.now())


class SafetyCheckinManager(abstract.Manager.from_queryset(SafetyCheckinQuerySet)):
    """Manager for SafetyCheckin."""
