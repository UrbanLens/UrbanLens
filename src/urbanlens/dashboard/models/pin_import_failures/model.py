"""PinImportFailure model - a pin from an import Google could not automatically place."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, SET_NULL, CharField, DecimalField, ForeignKey, Index, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_import_failures.queryset import PinImportFailureManager
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH


class PinImportFailureReason(abstract.TextChoices):
    """Why a pin's Google Maps CID could not be resolved to a location automatically."""

    NO_LOCATION_FOUND = "no_location_found", "Google has no location data for this place"
    LOOKUP_STALLED = "lookup_stalled", "Lookup didn't finish in time"
    LOOKUP_ERROR = "lookup_error", "Location lookup service was unreachable"


class PinImportFailureStatus(abstract.TextChoices):
    """Lifecycle status of a PinImportFailure."""

    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"
    DISMISSED = "dismissed", "Dismissed"


class PinImportFailure(abstract.DashboardModel):
    """A pin from an import whose Google Maps CID could not be automatically resolved to a location.

    Raised by ``tasks.resolve_deferred_pin_locations`` (via
    ``services.pin_import_failures.record_pin_import_failure``) when Google/REData
    either confirms no location exists for a cid, or repeated lookup attempts stall
    out - see that task's docstring. This always reflects a data gap on Google's side
    (or a transient lookup failure), never a defect in the pin the user tried to
    import - the review queue built on this model
    (``controllers.pin_import_failures``) must always phrase it that way to the owner.

    Unique per ``(profile, cid)``: re-running the same import must never create a
    second entry for a cid that already has one, regardless of its current status -
    see ``record_pin_import_failure``'s own docstring.

    Attributes:
        profile: Owner this failure belongs to.
        cid: The Google Maps CID that could not be resolved.
        name: Best-known name for the place, captured from the import at the moment
            it was deferred (the source row's own name/title). Since the cid never
            resolved, this may be the only information ever recorded about what this
            pin was supposed to be.
        description: Best-known description, captured the same way.
        reason: Why the automatic lookup failed.
        status: Whether this entry still needs the owner's attention.
        pin: The pin created once the owner supplies a location, if any.
    """

    cid = DecimalField(max_digits=20, decimal_places=0)
    name = CharField(max_length=255, blank=True, default="")
    description = TextField(blank=True, default="", max_length=MAX_PIN_DESCRIPTION_LENGTH, validators=[MaxLengthValidator(MAX_PIN_DESCRIPTION_LENGTH)])
    reason = CharField(max_length=20, choices=PinImportFailureReason.choices)
    status = CharField(max_length=20, choices=PinImportFailureStatus.choices, default=PinImportFailureStatus.PENDING)

    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="pin_import_failures")
    pin = ForeignKey("dashboard.Pin", on_delete=SET_NULL, null=True, blank=True, related_name="import_failure")

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int | None

    objects = PinImportFailureManager()

    @property
    def is_actionable(self) -> bool:
        """Whether this entry is still awaiting a response.

        Returns:
            True when status is pending.
        """
        return self.status == PinImportFailureStatus.PENDING

    def __str__(self) -> str:
        """Return a human-readable description of this failure.

        Returns:
            String like "Pin import failure for <profile_id>: cid <cid>".
        """
        return f"Pin import failure for {self.profile_id}: cid {self.cid}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_import_failures"
        indexes = [
            Index(fields=["profile", "status"], name="idxdb_pin_import_fail_status"),
        ]
        constraints = [
            UniqueConstraint(fields=["profile", "cid"], name="uq_pin_import_failure_profile_cid"),
        ]
