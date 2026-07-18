"""QuerySet and Manager for DirectMessage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from django.db.models import Case, Count, F, IntegerField, Max, Q, When
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.direct_messages.meta import RETENTION_DELTAS, MessageRetentionChoice

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

        The navbar label shows this (one label per conversation needing
        attention), while each dropdown row still shows its own per-conversation
        unread message count.

        Args:
            profile: The recipient profile.

        Returns:
            The number of distinct senders with an unread message to `profile`.
        """
        return self.unread_for(profile).values("sender_id").distinct().count()

    def due_for_hard_delete(self) -> Self:
        """Return messages whose disappearing-message timer has fully elapsed.

        Mirrors ``DirectMessage.is_expired_for_recipient`` (same read_at + delta
        threshold per ``sender_delete_after``), but as a queryset filter so a
        sweep task can physically delete the rows rather than just hiding them
        from the recipient's view. ``NEVER`` messages and unread messages are
        never included - the timer only starts once the recipient reads it.

        Returns:
            Messages ready for permanent deletion.
        """
        now = timezone.now()
        q = Q(sender_delete_after=MessageRetentionChoice.WHEN_READ, read_at__isnull=False)
        for choice, delta in RETENTION_DELTAS.items():
            q |= Q(sender_delete_after=choice, read_at__lte=now - delta)
        return self.filter(q)

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


class DirectMessageMuteQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for DirectMessageMute models."""

    def for_pair(self, viewer: Profile, sender: Profile) -> DirectMessageMuteQuerySet:
        """The mute row (at most one - unique on viewer+sender) for a pair.

        Row existence IS the mute state; callers chain ``.exists()`` to check
        it or ``.delete()`` to unmute.

        Args:
            viewer: The profile who muted.
            sender: The profile whose messages are muted.

        Returns:
            A queryset matching at most one row.
        """
        return self.filter(viewer=viewer, sender=sender)


class DirectMessageMuteManager(abstract.DashboardManager.from_queryset(DirectMessageMuteQuerySet)):
    """Custom query manager for DirectMessageMute models."""


class DirectMessageImagePermissionQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for DirectMessageImagePermission models."""

    def for_pair(self, viewer: Profile, sender: Profile) -> DirectMessageImagePermissionQuerySet:
        """The image-permission row (at most one - unique on viewer+sender) for a pair.

        Args:
            viewer: The profile deciding whether to see images.
            sender: The profile sending them.

        Returns:
            A queryset matching at most one row.
        """
        return self.filter(viewer=viewer, sender=sender)


class DirectMessageImagePermissionManager(abstract.DashboardManager.from_queryset(DirectMessageImagePermissionQuerySet)):
    """Custom query manager for DirectMessageImagePermission models."""
