from __future__ import annotations

from django.utils import timezone

from urbanlens.dashboard.models import abstract


class ProfileQuerySet(abstract.PublicDashboardQuerySet):
    """
    A custom queryset. All models below will use this for interacting with results from the db.
    """

    def pending_deletion(self):
        """Profiles currently soft-deleted and awaiting the hard delete."""
        return self.filter(deletion_requested_at__isnull=False)

    def due_for_deletion_reminder(self):
        """Profiles whose hard delete is 1 day out and haven't been reminded yet."""
        from urbanlens.dashboard.models.profile.model import ACCOUNT_DELETION_GRACE_PERIOD, ACCOUNT_DELETION_REMINDER_LEAD

        now = timezone.now()
        return self.filter(
            deletion_requested_at__isnull=False,
            deletion_requested_at__lte=now - (ACCOUNT_DELETION_GRACE_PERIOD - ACCOUNT_DELETION_REMINDER_LEAD),
            deletion_reminder_sent_at__isnull=True,
        )

    def due_for_hard_delete(self):
        """Profiles whose grace period has fully elapsed and must be hard-deleted now."""
        from urbanlens.dashboard.models.profile.model import ACCOUNT_DELETION_GRACE_PERIOD

        now = timezone.now()
        return self.filter(
            deletion_requested_at__isnull=False,
            deletion_requested_at__lte=now - ACCOUNT_DELETION_GRACE_PERIOD,
        )


class ProfileManager(abstract.PublicDashboardManager.from_queryset(ProfileQuerySet)):
    """
    A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
    """
