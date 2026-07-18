from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


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


class ProfileNoteQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for ProfileNote models."""

    def for_pair(self, author: Profile, subject: Profile) -> ProfileNoteQuerySet:
        """Notes a specific author has written about a specific subject.

        Args:
            author: The profile who wrote the note(s).
            subject: The profile the note(s) are about.

        Returns:
            Matching notes - a viewer may hold several per subject.
        """
        return self.filter(author=author, subject=subject)


class ProfileNoteManager(abstract.DashboardManager.from_queryset(ProfileNoteQuerySet)):
    """Custom query manager for ProfileNote models."""


class ProfileNicknameQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for ProfileNickname models."""

    def for_pair(self, author: Profile, subject: Profile) -> ProfileNicknameQuerySet:
        """The nickname row (at most one) an author has assigned to a subject.

        Args:
            author: The profile who assigned the nickname.
            subject: The profile the nickname is about.

        Returns:
            A queryset matching at most one row (unique on author+subject).
        """
        return self.filter(author=author, subject=subject)


class ProfileNicknameManager(abstract.DashboardManager.from_queryset(ProfileNicknameQuerySet)):
    """Custom query manager for ProfileNickname models."""


class ProfileTrustQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for ProfileTrust models."""

    def for_pair(self, author: Profile, subject: Profile) -> ProfileTrustQuerySet:
        """The trust rating row (at most one) an author has given a subject.

        Args:
            author: The profile who gave the rating.
            subject: The profile the rating is about.

        Returns:
            A queryset matching at most one row (unique on author+subject).
        """
        return self.filter(author=author, subject=subject)


class ProfileTrustManager(abstract.DashboardManager.from_queryset(ProfileTrustQuerySet)):
    """Custom query manager for ProfileTrust models."""
