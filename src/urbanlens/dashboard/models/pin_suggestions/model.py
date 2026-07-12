"""PinSuggestion model - a proposed pin visit or new pin awaiting the owner's confirmation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, DecimalField, ForeignKey, Index, JSONField, PositiveIntegerField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_suggestions.queryset import PinSuggestionManager

#: Distinct visit dates kept per suggestion - a sanity cap on storage/UI size,
#: not an API-call budget like ``services.photo_import.MAX_VISIT_DATES``.
MAX_STORED_VISIT_DATES = 30


class PinSuggestionOrigin(abstract.TextChoices):
    """What kind of batch scan raised a PinSuggestion."""

    IMMICH = "immich", "Immich library scan"
    LOCAL_SCAN = "local_scan", "Local folder scan"


class PinSuggestionStatus(abstract.TextChoices):
    """Lifecycle status of a PinSuggestion."""

    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class PinSuggestion(abstract.DashboardModel):
    """A place a batch photo scan (Immich library sweep or local folder scan) found evidence of visiting.

    Unlike :class:`~urbanlens.dashboard.models.visit_suggestions.model.VisitSuggestion`,
    this is always self-directed (no recipient/notification-preference routing) and may
    propose creating a brand-new pin rather than only logging a visit on one that already
    exists. Created in bulk by ``services.pin_suggestions.ingest_location_hits``, which
    matches each discovered coordinate against the profile's existing pins (their
    effective property boundary, exactly as ``services.visits.find_pin_containing_point``
    does for live geolocation) and clusters whatever doesn't match into new-pin candidates.

    Attributes:
        profile: Owner this suggestion belongs to and would create pins/visits for.
        pin: The profile's existing pin this cluster falls within, if any. When set,
            accepting logs visit(s) on it. When null, accepting creates a new pin here.
        location: Shared Location for the place, resolved only on accept (coordinates
            are immutable once a Location exists, so this stays unset while pending -
            rejecting a suggestion should never leave behind a Location row).
        latitude: Cluster centroid latitude.
        longitude: Cluster centroid longitude.
        origin: Which batch scan raised this suggestion.
        status: Whether this suggestion is pending, accepted, or rejected.
        visit_dates: Distinct ISO ``YYYY-MM-DD`` dates seen for this cluster, capped at
            ``MAX_STORED_VISIT_DATES``. One PinVisit is created per date on accept.
        hit_count: Total number of source photos/assets that fed this cluster (may
            exceed ``len(visit_dates)`` when multiple photos share a date).
        suggested_name: A place-name guess (e.g. Immich's reverse-geocoded city) to
            offer as the new pin's name. Only applied on accept, and only when the
            target pin has no name of its own yet.
    """

    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    origin = CharField(max_length=20, choices=PinSuggestionOrigin.choices)
    status = CharField(max_length=20, choices=PinSuggestionStatus.choices, default=PinSuggestionStatus.PENDING)
    visit_dates = JSONField(default=list)
    hit_count = PositiveIntegerField(default=1)
    suggested_name = CharField(max_length=255, blank=True, default="")

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="pin_suggestions")
    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="pin_suggestions")
    location = ForeignKey("dashboard.Location", on_delete=SET_NULL, null=True, blank=True, related_name="pin_suggestions")

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int | None
        location_id: int | None

    objects = PinSuggestionManager()

    @property
    def is_actionable(self) -> bool:
        """Whether this suggestion is still awaiting a response.

        Returns:
            True when status is pending.
        """
        return self.status == PinSuggestionStatus.PENDING

    @property
    def is_new_pin(self) -> bool:
        """Whether accepting this suggestion would create a brand-new pin.

        Returns:
            True when no existing pin matched this cluster.
        """
        return self.pin_id is None

    def __str__(self) -> str:
        """Return a human-readable description of this suggestion.

        Returns:
            String like "Pin suggestion for <profile_id> at <lat>,<lon>".
        """
        return f"Pin suggestion for {self.profile_id} at {self.latitude},{self.longitude}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_suggestions"
        indexes = [
            Index(fields=["profile", "status"], name="idxdb_pin_sugg_status"),
        ]
