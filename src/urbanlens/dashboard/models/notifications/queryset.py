"""QuerySet and Manager for NotificationLog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.services.social.friendship import MutedRecipients


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

    def notify(self, *, muted_recipients: MutedRecipients | None = None, **fields) -> NotificationLog | None:
        """Create a notification unless its recipient has muted its source.

        **The sanctioned way to raise a notification.** ``create()`` still
        works and is what this calls, but it applies no preference at all, so
        anything reaching for it bypasses mute silently -
        ``bin/check_notification_choke_point.py`` fails the build for a
        production call site that does. That check exists because the previous
        arrangement had no choke point: every producer would have had to
        remember the rule independently, and the result was that
        ``Friendship``'s mute flag was written faithfully by two UI surfaces
        and read by nothing for months.

        Delivery to every other channel follows from the row: the live
        WebSocket toast, the WhatsApp/SMS alert and the native push all hang
        off ``post_save`` on ``NotificationLog`` (see
        ``models.notifications.signals``), so not writing it is what actually
        produces silence. Emails sent directly by a producer alongside its
        notification are *not* covered - they never passed through here.

        Args:
            muted_recipients: Pre-resolved answer for callers notifying a
                whole membership, from
                ``services.social.friendship.profiles_muting``. Purely a query
                saving: forgetting it costs one indexed ``SELECT`` per
                notification and changes nothing about the outcome, which is
                the right way round for an optimisation to be optional. It
                carries the source it was resolved against, and one resolved
                for a *different* source is ignored rather than trusted -
                reusing a batch across senders would otherwise silence the
                wrong notifications, silently and only in the batched paths.
            **fields: Model fields, exactly as ``create()`` takes them.
                ``profile`` (recipient) and ``source_profile`` (who the
                notification is about) are what the mute check reads, in either
                spelling - ``profile``/``profile_id``; a notification with no
                source is nobody's to mute.

        Returns:
            The new notification, or None when it was suppressed. Callers that
            store the row (``PinShare.notification`` and friends) already
            handle None - that FK is nullable because a recipient can switch
            the type off entirely, which produces the same outcome.
        """
        from urbanlens.dashboard.models.notifications.meta import MUTE_EXEMPT_TYPES

        if fields.get("notification_type") not in MUTE_EXEMPT_TYPES:
            # Both spellings, because either is a legitimate way to write the
            # same row and reading only one would make the preference depend on
            # how a producer happened to hold its profiles.
            recipient = fields.get("profile_id") if fields.get("profile") is None else fields["profile"].pk
            source = fields.get("source_profile_id") if fields.get("source_profile") is None else fields["source_profile"].pk
            if muted_recipients is not None and muted_recipients.source_id == source:
                if recipient in muted_recipients.profile_ids:
                    return None
            else:
                from urbanlens.dashboard.services.social.friendship import notifications_muted

                if notifications_muted(recipient, source):
                    return None
        return self.create(**fields)
