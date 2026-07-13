"""QuerySet and Manager for DirectMessage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from django.db.models import Case, Count, F, IntegerField, Max, Q, When
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.direct_messages.model import DirectMessage
    from urbanlens.dashboard.models.profile.model import Profile


class DirectMessageQuerySet(abstract.DashboardQuerySet):
    """QuerySet for DirectMessage with conversation-oriented helpers."""

    def involving(self, profile: Profile) -> Self:
        """Return every message the given profile sent or received.

        Args:
            profile: The profile whose messages to return.

        Returns:
            Messages where the profile is the sender or the recipient.
        """
        return self.filter(Q(sender=profile) | Q(recipient=profile))

    def between(self, profile: Profile, other: Profile) -> Self:
        """Return the full two-way conversation between two profiles.

        Args:
            profile: One participant.
            other: The other participant.

        Returns:
            Messages sent in either direction between the two profiles,
            in chronological order (the model's default ordering).
        """
        return self.filter(
            Q(sender=profile, recipient=other) | Q(sender=other, recipient=profile),
        )

    def visible_to(self, profile: Profile) -> Self:
        """Exclude messages `profile` has removed from their own view.

        A message deleted "for everyone" by its sender stays visible here as
        a tombstone (rendered as removed text, not excluded); this only hides
        a message the *viewing* profile chose to delete for themselves alone.

        Args:
            profile: The viewing profile.

        Returns:
            Messages sent (and not self-deleted) or received (and not
            self-deleted) by `profile`.
        """
        return self.filter(Q(sender=profile, deleted_by_sender_at__isnull=True) | Q(recipient=profile, deleted_by_recipient_at__isnull=True))

    def unread_for(self, profile: Profile) -> Self:
        """Return messages addressed to the profile that have not been read yet.

        Args:
            profile: The recipient profile.

        Returns:
            Unread messages where the profile is the recipient.
        """
        return self.filter(recipient=profile, read_at__isnull=True)

    def unread_conversation_count(self, profile: Profile) -> int:
        """Count distinct conversations with at least one unread message.

        The navbar badge shows this (one badge per conversation needing
        attention), while each dropdown row still shows its own per-conversation
        unread message count.

        Args:
            profile: The recipient profile.

        Returns:
            The number of distinct senders with an unread message to `profile`.
        """
        return self.unread_for(profile).values("sender_id").distinct().count()

    def mark_read(self) -> int:
        """Mark every unread message in this queryset as read now.

        Returns:
            The number of rows updated.
        """
        return self.filter(read_at__isnull=True).update(read_at=timezone.now())

    def conversation_rows(self, profile: Profile) -> QuerySet[DirectMessage, dict[str, Any]]:
        """Aggregate the profile's messages into one row per conversation partner.

        Args:
            profile: The profile whose conversations to summarize.

        Returns:
            A values queryset with ``partner_id`` (the other profile's pk),
            ``last_message_id`` (pk of the newest message either way), and
            ``unread_count`` (messages from the partner not yet read), ordered
            most-recently-active first.
        """
        partner = Case(
            When(sender=profile, then=F("recipient_id")),
            default=F("sender_id"),
            output_field=IntegerField(),
        )
        return (
            self.involving(profile)
            .annotate(partner_id=partner)
            .values("partner_id")
            .annotate(
                last_message_id=Max("id"),
                unread_count=Count("id", filter=Q(recipient=profile, read_at__isnull=True)),
            )
            .order_by("-last_message_id")
        )


class DirectMessageManager(abstract.DashboardManager.from_queryset(DirectMessageQuerySet)):
    """Manager for DirectMessage."""
