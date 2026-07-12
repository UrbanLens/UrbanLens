from __future__ import annotations

import datetime

from urbanlens.dashboard.models.abstract.choices import TextChoices


class MessageRetentionChoice(TextChoices):
    """How long after being read a sent direct message disappears from the recipient's view.

    Chosen by the sender (``Profile.direct_message_delete_after``) and
    snapshotted onto each ``DirectMessage.sender_delete_after`` at send time,
    so changing the setting later only affects messages sent afterward. Never
    affects the sender's own view of their own messages.
    """

    NEVER = "never", "Never"
    WHEN_READ = "when_read", "As soon as they're read"
    ONE_DAY = "one_day", "1 day after being read"
    THIRTY_DAYS = "thirty_days", "30 days after being read"
    NINETY_DAYS = "ninety_days", "90 days after being read"
    ONE_YEAR = "one_year", "1 year after being read"


# Delay applied after `read_at` before a message becomes expired for the
# recipient. WHEN_READ and NEVER are handled as special cases by the caller.
RETENTION_DELTAS: dict[str, datetime.timedelta] = {
    MessageRetentionChoice.ONE_DAY: datetime.timedelta(days=1),
    MessageRetentionChoice.THIRTY_DAYS: datetime.timedelta(days=30),
    MessageRetentionChoice.NINETY_DAYS: datetime.timedelta(days=90),
    MessageRetentionChoice.ONE_YEAR: datetime.timedelta(days=365),
}


class DirectMessageShareKind(TextChoices):
    """What kind of `@`-mention share a message carries."""

    PIN = "pin", "Pin"
    TRIP = "trip", "Trip"
    FRIEND = "friend", "Friend recommendation"


class ImagePermissionStatus(TextChoices):
    """A viewer's standing decision on whether to see images from a given sender."""

    PENDING = "pending", "Pending"
    ALLOWED = "allowed", "Allowed"
    REJECTED = "rejected", "Rejected"
