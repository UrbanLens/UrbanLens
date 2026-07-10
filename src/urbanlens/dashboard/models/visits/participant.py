"""ExternalVisitParticipant - a visit participant who is not (yet) a site member.

Lets a pin owner record everyone who was present on a visit, not just
connected members: an external participant is just a display name, with an
optional one-way hash of their email address. The raw address is never
stored - the person has not consented to being in our database - but the
hash lets us recognise them if they ever register (or verify a matching
secondary email), at which point the deferred friend request and visit
suggestion are delivered (see ``services.visit_invites``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, ForeignKey, Index

from urbanlens.dashboard.models import abstract


class ExternalVisitParticipant(abstract.DashboardModel):
    """A non-member participant the owner added to one of their pin visits.

    Attributes:
        visit: The PinVisit this person took part in.
        display_name: Name the owner entered for this person.
        email_hash: SHA-256 hash of the person's normalized email address, or
            empty when no email was provided. Used to match a future account.
        invite_sent: Whether a join-the-site email was actually sent for this
            row (rate caps or the one-invite-per-address rule may suppress it).
        suggestion_requested: Whether the owner asked for a visit suggestion
            to be delivered to this person (immediately when the email already
            belongs to a member, otherwise once they register).
        matched_profile: The member account this row was resolved to, either
            at creation time (email already registered) or later at sign-up.
    """

    display_name = CharField(max_length=100)
    email_hash = CharField(max_length=64, blank=True, default="")
    invite_sent = BooleanField(default=False)
    suggestion_requested = BooleanField(default=False)

    visit = ForeignKey(
        "dashboard.PinVisit",
        on_delete=CASCADE,
        related_name="external_participants",
    )
    matched_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="external_visit_participations",
    )

    if TYPE_CHECKING:
        visit_id: int
        matched_profile_id: int | None

    def __str__(self) -> str:
        return f"{self.display_name} on visit {self.visit_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_external_visit_participants"
        indexes = [
            Index(fields=["email_hash"], name="idxdb_evp_email_hash"),
            Index(fields=["visit"], name="idxdb_evp_visit"),
        ]
