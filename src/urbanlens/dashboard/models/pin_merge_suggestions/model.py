"""PinMergeSuggestion model - a proposal that two of a profile's own pins are the same place."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, CheckConstraint, F, ForeignKey, Index, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_merge_suggestions.queryset import PinMergeSuggestionManager

#: Max length of PinMergeSuggestion.reason - a short caption, not a full report.
MAX_REASON_LENGTH = 500


class PinMergeSuggestionOrigin(abstract.TextChoices):
    """What raised a PinMergeSuggestion."""

    LEGACY_CID_COLLISION = "legacy_cid_collision", "Legacy CID coordinate repair collision"


class PinMergeSuggestionStatus(abstract.TextChoices):
    """Lifecycle status of a PinMergeSuggestion."""

    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class PinMergeSuggestion(abstract.DashboardModel):
    """A proposal that two of a profile's own pins are the same place and should merge.

    Always self-directed - pin_a and pin_b always belong to the same profile
    (never a cross-profile suggestion; unlike VisitSuggestion, this never routes
    through a NotificationLog to another user). pin_a/pin_b carry no ordering
    meaning - either may end up the survivor; see ``suggested_survivor`` for the
    precomputed recommendation the UI preselects, which the accepting user may
    always override.

    Attributes:
        profile: Owner of both pins.
        pin_a: One of the two pins under consideration.
        pin_b: The other pin under consideration.
        suggested_survivor: The pin recommended to survive the merge - either an
            origin-specific deterministic choice (e.g. the correctly-placed pin
            for a legacy CID collision) or a general heuristic default (more
            visit/photo history, then older). Always overridable at accept time.
        origin: What raised this suggestion.
        status: Whether this suggestion is pending, accepted, or rejected.
        reason: Short, human-readable explanation shown on the review-queue card.
    """

    origin = CharField(max_length=30, choices=PinMergeSuggestionOrigin.choices)
    status = CharField(max_length=20, choices=PinMergeSuggestionStatus.choices, default=PinMergeSuggestionStatus.PENDING)
    reason = TextField(blank=True, default="", max_length=MAX_REASON_LENGTH)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="pin_merge_suggestions")
    # SET_NULL, not CASCADE: accepting a suggestion deletes the losing pin,
    # and a suggestion always references its own loser via pin_a/pin_b - a
    # CASCADE here would delete the suggestion itself (the very row recording
    # that it was accepted) the instant the loser is deleted. SET_NULL just
    # drops the reference to the now-gone loser; the surviving pin's side
    # keeps pointing at a real, still-existing Pin.
    pin_a = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="merge_suggestions_as_a")
    pin_b = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="merge_suggestions_as_b")
    suggested_survivor = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="+")

    if TYPE_CHECKING:
        profile_id: int
        pin_a_id: int | None
        pin_b_id: int | None
        suggested_survivor_id: int | None

    objects = PinMergeSuggestionManager()

    @property
    def is_actionable(self) -> bool:
        """Whether this suggestion is still awaiting a response.

        Returns:
            True when status is pending.
        """
        return self.status == PinMergeSuggestionStatus.PENDING

    def __str__(self) -> str:
        """Return a human-readable description of this suggestion.

        Returns:
            String like "Pin merge suggestion for <profile_id>: <pin_a_id> + <pin_b_id>".
        """
        return f"Pin merge suggestion for {self.profile_id}: {self.pin_a_id} + {self.pin_b_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_merge_suggestions"
        indexes = [
            Index(fields=["profile", "status"], name="idxdb_pin_merge_sugg_status"),
        ]
        constraints = [
            CheckConstraint(condition=~Q(pin_a=F("pin_b")), name="db_pin_merge_suggestion_distinct_pins"),
        ]
