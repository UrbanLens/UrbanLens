"""Membership and note-offer rows behind shared placeless places (ADR35 section 2, UL-367)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, Index, TextChoices, UniqueConstraint

from urbanlens.dashboard.models import abstract


class SharedPlaceMember(abstract.DashboardModel):
    """
    A profile admitted to a provisional or anchored place by the Celery-only evaluator.

    Wiki access to a synthetic place is decided by this row and nothing else; a synthetic place never resolves onto a ``Location``, so no
    tier-0 wiki can be reached through it. Never serialise a row, or any count of rows, to anyone but its own profile.

    Attributes:
        place: The synthetic place.
        profile: The member.
    """

    place = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="shared_members")
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="shared_place_memberships")

    if TYPE_CHECKING:
        place_id: int
        profile_id: int

    def __str__(self) -> str:
        return f"SharedPlaceMember(place={self.place_id}, profile={self.profile_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_shared_place_members"
        constraints = [UniqueConstraint(fields=["place", "profile"], name="shared_place_member_unique")]
        indexes = [Index(fields=["profile", "created"], name="idxdb_spm_profile_created")]


class NotesOfferStatus(TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"


class NotesOffer(abstract.DashboardModel):
    """
    One member's invitation to bring their own exact-point notes into the (empty) shared wiki.

    Nothing is copied until the owner accepts. The only user action in the shared-place flow.

    Attributes:
        profile: The member whose notes these are; the only person who can see or answer the offer.
        place: The shared place whose wiki would receive the notes.
        source_location: The member's own tier-0 location, whose wiki holds the notes.
        status: Pending until the member answers.
        answered_at: When they did.
    """

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="notes_offers")
    place = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="notes_offers")
    source_location = ForeignKey("dashboard.Location", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    status = CharField(max_length=10, choices=NotesOfferStatus.choices, default=NotesOfferStatus.PENDING)
    answered_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        profile_id: int
        place_id: int

    def __str__(self) -> str:
        return f"NotesOffer({self.status}, profile={self.profile_id}, place={self.place_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_notes_offers"
        constraints = [UniqueConstraint(fields=["profile", "place"], name="notes_offer_unique")]
        indexes = [Index(fields=["profile", "status"], name="idxdb_notesoffer_prof_status")]
