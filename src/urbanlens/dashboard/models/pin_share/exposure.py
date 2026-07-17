"""LocationExposure - a durable record that a profile learned about a place via a share.

This is the "infection" half of share-chain tracking. A ``PinShare`` row alone
is not enough to keep reshare chains honest, because a recipient's Pin is
mutable while its Location is immutable:

- Tracking only the *pin* breaks when the recipient moves their pin elsewhere,
  then drops a brand-new pin at the original spot and shares that one.
- Tracking only the *location* breaks when the recipient moves their pin to a
  new location and shares it from there.

So a share "infects" both. Every share received creates a LocationExposure for
``(recipient, shared location)``, and moving a pin propagates its owner's
exposures from the old location to the new one (see ``Pin.save`` /
``services.share_provenance.propagate_exposures_for_pin_move``). Exposure rows
are deliberately independent of any Pin, so deleting and re-creating pins never
clears them: any future pin the recipient creates within
``services.share_provenance.EXPOSURE_RADIUS_METERS`` of an exposed location -
at any time - has its onward shares chained back to the originating share (see
``services.share_provenance.resolve_origin_share``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Index, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin_share.queryset import LocationExposureManager


class ExposureSource(abstract.TextChoices):
    """How a LocationExposure row came to exist."""

    # The profile received a PinShare whose place is this location.
    SHARE_RECEIVED = "share_received", "Share Received"
    # Propagated from another exposed location when the profile moved a pin
    # there - the infection follows the pin to its new location.
    PIN_MOVED = "pin_moved", "Pin Moved"


class LocationExposure(abstract.DashboardModel):
    """One profile's exposure to one Location through one share.

    Read as: "``profile`` first learned about ``location`` via ``share``".
    Multiple exposures may exist for the same (profile, location) when the
    place was shared with them repeatedly - resolution picks the earliest
    (see ``services.share_provenance.resolve_origin_share``).
    """

    profile = models.ForeignKey("dashboard.Profile", on_delete=models.CASCADE, related_name="location_exposures")
    location = models.ForeignKey("dashboard.Location", on_delete=models.CASCADE, related_name="exposures")
    # The share that (directly or transitively, via pin moves) delivered the
    # information. Onward shares by `profile` of this place chain under it.
    share = models.ForeignKey("dashboard.PinShare", on_delete=models.CASCADE, related_name="exposures")
    source = models.CharField(max_length=20, choices=ExposureSource.choices, default=ExposureSource.SHARE_RECEIVED)

    objects = LocationExposureManager()

    if TYPE_CHECKING:
        profile_id: int
        location_id: int
        share_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this exposure.

        Returns:
            String like "Exposure profile=3 location=9 share=12".
        """
        return f"Exposure profile={self.profile_id} location={self.location_id} share={self.share_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_location_exposures"
        indexes = [
            Index(fields=["profile", "location"], name="idxdb_locexp_pfl_loc"),
        ]
        constraints = [
            UniqueConstraint(fields=["profile", "location", "share"], name="db_locexp_one_per_pfl_loc_share"),
        ]
