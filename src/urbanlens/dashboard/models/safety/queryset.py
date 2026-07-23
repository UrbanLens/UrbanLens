"""Querysets and managers for the safety-checkin model family."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Self

from django.db.models import DateTimeField, ExpressionWrapper, F, Q
from django.db.models.functions import Greatest
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.safety.model import SafetyCheckin


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

    def due_for_auto_delete(self) -> Self:
        """Return resolved check-ins past their owner's auto-delete window, if any.

        The window is a per-profile ``SafetyPreference.auto_delete_after_days`` setting;
        a null value means "never auto-delete" and excludes the profile's check-ins here.

        The window counts from whichever is later, ``resolved_at`` or ``created`` -
        the undo-delete framework (``services.undo.handlers.safety_checkin``) recreates
        a restored check-in as a brand-new row carrying its *original* ``resolved_at``,
        so counting from ``resolved_at`` alone could make a just-restored check-in
        immediately due again on the next sweep, silently undoing the undo.

        Returns:
            Filtered queryset.
        """
        from urbanlens.dashboard.models.safety.model import SafetyCheckinStatus

        return (
            self.filter(
                status__in=SafetyCheckinStatus.resolved_statuses(),
                resolved_at__isnull=False,
                profile__safety_preference__auto_delete_after_days__isnull=False,
            )
            .annotate(
                delete_at=ExpressionWrapper(
                    Greatest(F("resolved_at"), F("created")) + F("profile__safety_preference__auto_delete_after_days") * timedelta(days=1),
                    output_field=DateTimeField(),
                ),
            )
            .filter(delete_at__lte=timezone.now())
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

    def shared_with(self, profile: Profile) -> Self:
        """Return other profiles' check-ins where ``profile`` is a registered emergency contact.

        Powers the safety overview's "Shared with you" section - a logged-in
        emergency contact gets a read-only view of the check-in (see
        ``SafetyCheckinDetailView._render_shared_view``) even before/without
        the owner ever posting it to a community wiki.

        Args:
            profile: The viewing profile.

        Returns:
            Filtered queryset, most recent ``checkin_by`` first, excluding the
            viewer's own check-ins.
        """
        return self.filter(contacts__contact_profile=profile).exclude(profile=profile).distinct()


class SafetyCheckinManager(abstract.PublicDashboardManager.from_queryset(SafetyCheckinQuerySet)):
    """Manager for SafetyCheckin."""


class SafetyCheckinContactQuerySet(abstract.DashboardQuerySet):
    """QuerySet for SafetyCheckinContact records."""

    def by_token(self, token: str) -> Self:
        """Resolve a contact by their magic-link token.

        A contact identified only by email has no account to log into, so
        the public contact portal (and the check-in/markup-map views it
        links to) all resolve the requesting contact this same way - see
        the model's own docstring for why ``token`` is the credential here.

        Args:
            token: The magic-link token from the URL.

        Returns:
            Queryset filtered to at most one matching row - callers typically
            wrap this in ``get_object_or_404`` (optionally after chaining
            their own ``select_related(...)`` first).
        """
        return self.filter(token=token)


class SafetyCheckinContactManager(abstract.DashboardManager.from_queryset(SafetyCheckinContactQuerySet)):
    """Manager for SafetyCheckinContact."""


class EmergencyContactDefaultQuerySet(abstract.DashboardQuerySet):
    """QuerySet for EmergencyContactDefault records."""

    def for_owner(self, owner: Profile) -> Self:
        """Return a profile's saved default emergency contacts.

        Args:
            owner: The profile whose defaults to return.

        Returns:
            Filtered queryset, in the model's default ``order``/``created`` ordering.
        """
        return self.filter(owner=owner)


class EmergencyContactDefaultManager(abstract.DashboardManager.from_queryset(EmergencyContactDefaultQuerySet)):
    """Manager for EmergencyContactDefault."""


class SafetyContactOptOutQuerySet(abstract.DashboardQuerySet):
    """QuerySet for SafetyContactOptOut records."""


class SafetyContactOptOutManager(abstract.DashboardManager.from_queryset(SafetyContactOptOutQuerySet)):
    """Manager for SafetyContactOptOut."""

    def blocks_notification(
        self,
        contact_profile: Profile | None,
        email: str | None,
        *,
        owner: Profile,
        checkin: SafetyCheckin | None = None,
    ) -> bool:
        """Whether a contact identity has opted out of notifications relevant to this owner/check-in.

        Args:
            contact_profile: The contact's profile, if they have an account.
            email: The contact's email, used to resolve identity when ``contact_profile`` is None.
            owner: The check-in owner whose notification is about to be sent.
            checkin: The specific check-in being notified about, if any - enables matching a
                CHECKIN-scoped opt-out in addition to OWNER/GLOBAL-scoped ones.

        Returns:
            True if a matching GLOBAL, OWNER, or (when ``checkin`` is given) CHECKIN-scoped
            opt-out row exists for this contact identity.
        """
        from urbanlens.dashboard.models.safety.model import SafetyContactOptOutScope

        identity = Q(contact_profile=contact_profile) if contact_profile else Q(email__iexact=email)
        scope = Q(scope=SafetyContactOptOutScope.GLOBAL) | Q(scope=SafetyContactOptOutScope.OWNER, owner=owner)
        if checkin is not None:
            scope |= Q(scope=SafetyContactOptOutScope.CHECKIN, checkin=checkin)
        return self.filter(identity & scope).exists()
