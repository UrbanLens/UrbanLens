"""VisitSuggestion model - a proposed PinVisit awaiting confirmation from another user."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import CheckConstraint, Index, Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.visit_suggestions.queryset import VisitSuggestionManager


class VisitSuggestionStatus(abstract.TextChoices):
    """Lifecycle status of a VisitSuggestion."""

    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class VisitSuggestion(abstract.Model):
    """A proposed PinVisit sent to another user for confirmation.

    Created when a user tags a connection as a co-visitor in the visit-add dialog
    (``origin_visit`` set), when a trip activity is marked completed and another
    RSVP'd-yes member needs to confirm they were there (``trip_activity`` set), or
    when a safety check-in concludes and the checked-in user needs to confirm they
    actually made it to the planned destination (``safety_checkin`` set). Exactly
    one of those three links is set per row.

    Only ``location``/``latitude``/``longitude``/``visited_at`` are used to identify
    the place and time to the recipient - the origin pin's private custom name and
    visit notes are never referenced here or in the notification built from this row.

    Attributes:
        location: Shared Location identifying the place, if one exists.
        latitude: Latitude of the place, always present regardless of location.
        longitude: Longitude of the place, always present regardless of location.
        visited_at: When the visit is claimed to have occurred.
        suggested_by: Profile who proposed this suggestion, if known.
        suggested_to: Profile being asked to confirm the visit.
        origin_visit: The suggester's own PinVisit this suggestion was raised from.
        trip_activity: The completed TripActivity this suggestion was raised from.
        safety_checkin: The concluded SafetyCheckin this suggestion was raised from.
        candidate_profiles: Other profiles from the same batch (minus suggested_to),
            re-filtered to mutual connections of suggested_to at accept time.
        notification: The notification delivered to suggested_to for this row.
        status: Whether this suggestion is pending, accepted, or rejected.
        existing_visit: suggested_to's own PinVisit already logged for this place
            and date, if one exists. When set, accepting offers a choice between
            merging the new participants into this visit or logging a separate one,
            instead of the plain accept/reject shown for a first-time suggestion.
    """

    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    visited_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=VisitSuggestionStatus.choices, default=VisitSuggestionStatus.PENDING)

    location = models.ForeignKey(
        "dashboard.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="visit_suggestions",
    )
    suggested_by = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_visit_suggestions",
    )
    suggested_to = models.ForeignKey(
        "dashboard.Profile",
        on_delete=models.CASCADE,
        related_name="received_visit_suggestions",
    )

    origin_visit = models.ForeignKey(
        "dashboard.PinVisit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="suggestions_sent",
    )
    trip_activity = models.ForeignKey(
        "dashboard.TripActivity",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="visit_suggestions",
    )
    safety_checkin = models.ForeignKey(
        "dashboard.SafetyCheckin",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="visit_suggestions",
    )

    candidate_profiles = models.ManyToManyField("dashboard.Profile", blank=True, related_name="+")

    existing_visit = models.ForeignKey(
        "dashboard.PinVisit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merge_suggestions",
    )

    notification = models.OneToOneField(
        "dashboard.NotificationLog",
        on_delete=models.SET_NULL,
        related_name="visit_suggestion",
        null=True,
        blank=True,
    )

    if TYPE_CHECKING:
        location_id: int | None
        suggested_by_id: int | None
        suggested_to_id: int
        origin_visit_id: int | None
        trip_activity_id: int | None
        safety_checkin_id: int | None
        existing_visit_id: int | None
        notification_id: int | None

    objects = VisitSuggestionManager()

    @property
    def is_actionable(self) -> bool:
        """Whether this suggestion is still awaiting a response.

        Returns:
            True when status is pending.
        """
        return self.status == VisitSuggestionStatus.PENDING

    @property
    def offers_merge(self) -> bool:
        """Whether accepting this suggestion should offer a merge-or-separate choice.

        Returns:
            True when suggested_to already has a same-day visit at this place.
        """
        return self.existing_visit_id is not None

    def __str__(self) -> str:
        """Return a human-readable description of this suggestion.

        Returns:
            String like "Visit suggestion to <suggested_to_id> on YYYY-MM-DD".
        """
        return f"Visit suggestion to {self.suggested_to_id} on {self.visited_at:%Y-%m-%d}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_visit_suggestions"
        indexes = [
            Index(fields=["suggested_to", "status"], name="idxdb_visit_st_status"),
        ]
        constraints = [
            CheckConstraint(
                # Exactly one of the three origin links must be set. CheckConstraint's ``^``
                # only XORs two Q objects, so a third origin needs the explicit
                # one-true-the-other-two-false form instead.
                condition=(
                    (Q(origin_visit__isnull=False) & Q(trip_activity__isnull=True) & Q(safety_checkin__isnull=True))
                    | (Q(origin_visit__isnull=True) & Q(trip_activity__isnull=False) & Q(safety_checkin__isnull=True))
                    | (Q(origin_visit__isnull=True) & Q(trip_activity__isnull=True) & Q(safety_checkin__isnull=False))
                ),
                name="db_visit_suggestion_exactly_one_origin",
            ),
        ]
