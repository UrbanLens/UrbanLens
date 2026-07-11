"""EmailSendLog - privacy-preserving record of user-triggered outbound emails.

Every email a user causes the site to send to a third party (join-the-site
invitations, visit invites, ...) is logged here so that per-user send caps can
be enforced and duplicate "join the site" emails to the same address can be
suppressed.

The recipient's address is stored only as a one-way hash of its normalized
form: the recipient has not consented to having their address stored, and a
hash is all that rate limiting and duplicate detection need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, Index

from urbanlens.dashboard.models import abstract


class EmailType(abstract.TextChoices):
    """What kind of email a user caused the site to send.

    Attributes:
        JOIN_INVITE: "Join the site" invitation from the invite-a-friend flow.
        VISIT_INVITE: "Join the site" invitation raised by tagging a
            non-member (by email) as a visit participant.
    """

    JOIN_INVITE = "join_invite", "Friend invitation"
    VISIT_INVITE = "visit_invite", "Visit participant invitation"


# Email types that invite the recipient to join the site. A given user sends
# at most one of these to a given address, ever (see services.email_safety).
JOIN_EMAIL_TYPES: tuple[str, ...] = (EmailType.JOIN_INVITE, EmailType.VISIT_INVITE)


class EmailSendLog(abstract.DashboardModel):
    """One user-triggered outbound email to a third-party address.

    Attributes:
        sender: The profile whose action caused the email to be sent.
        recipient_hash: SHA-256 hash of the normalized recipient address
            (see :func:`urbanlens.dashboard.services.email_safety.hash_email`).
            The raw address is never stored.
        email_type: What kind of email was sent.
    """

    sender = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="email_send_logs",
    )
    recipient_hash = CharField(max_length=64)
    email_type = CharField(max_length=20, choices=EmailType.choices)

    if TYPE_CHECKING:
        sender_id: int

    def __str__(self) -> str:
        return f"EmailSendLog({self.sender_id}, {self.email_type}, {self.created:%Y-%m-%d})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_email_send_logs"
        indexes = [
            Index(fields=["sender", "created"], name="idxdb_esl_sender_created"),
            Index(fields=["sender", "recipient_hash"], name="idxdb_esl_sender_recipient"),
        ]
