"""QuerySet and manager for SafetyCheckin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone

from urbanlens.dashboard.models import abstract


class SafetyCheckinQuerySet(abstract.PublicDashboardQuerySet):
    """QuerySet for SafetyCheckin records."""

    def due_for_reminder(self) -> Self:
        """Return scheduled check-ins whose expected check-in time has arrived.

        Excludes rows already past their grace period - those belong to
        ``overdue()`` instead, whether or not the reminder ever went out.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        now = timezone.now()
        return self.annotate(
            overdue_at=ExpressionWrapper(F("checkin_by") + F("grace_period"), output_field=DateTimeField()),
        ).filter(status=SafetyCheckinStatus.SCHEDULED, checkin_by__lte=now, overdue_at__gt=now)

    def overdue(self) -> Self:
        """Return check-ins whose grace period has elapsed with no response.

        Includes check-ins still stuck on SCHEDULED (not just AWAITING_CHECKIN) so a
        missed or failed ``send_due_checkin_reminders`` run can't prevent escalation -
        the reminder-send transitions status to AWAITING_CHECKIN only after the
        notification succeeds (see ``services.safety.send_checkin_reminder``).

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        return self.annotate(
            overdue_at=ExpressionWrapper(F("checkin_by") + F("grace_period"), output_field=DateTimeField()),
        ).filter(
            status__in=(SafetyCheckinStatus.SCHEDULED, SafetyCheckinStatus.AWAITING_CHECKIN),
            overdue_at__lte=timezone.now(),
        )

    def due_for_final_warning(self) -> Self:
        """Return awaiting check-ins about to escalate to emergency contacts.

        Catches check-ins within ``FINAL_WARNING_LEAD_TIME`` of their overdue
        point that haven't already gotten a final warning - once escalated,
        ``overdue()`` takes over and this no longer matches (status moves off
        AWAITING_CHECKIN).

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import FINAL_WARNING_LEAD_TIME, SafetyCheckinStatus

        now = timezone.now()
        return self.annotate(
            overdue_at=ExpressionWrapper(F("checkin_by") + F("grace_period"), output_field=DateTimeField()),
        ).filter(
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            final_warning_sent_at__isnull=True,
            overdue_at__gt=now,
            overdue_at__lte=now + FINAL_WARNING_LEAD_TIME,
        )

    def active(self) -> Self:
        """Return check-ins that have not yet reached a terminal status.

        Used to enforce that a profile may only have one active check-in at a
        time (see ``services.safety.create_checkin``) and to power the
        navbar's active-check-in banner.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        return self.exclude(status__in=SafetyCheckinStatus.resolved_statuses())


class SafetyCheckinManager(abstract.PublicDashboardManager.from_queryset(SafetyCheckinQuerySet)):
    """Manager for SafetyCheckin."""
