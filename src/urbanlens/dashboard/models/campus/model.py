"""Campus model - defines the spatial region for a Location or Pin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import MultiPolygonField
from django.contrib.gis.geos import Point
from django.db.models import CASCADE, ForeignKey, IntegerField, Q
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.campus.queryset import CampusManager

logger = logging.getLogger(__name__)

_DEFAULT_RADIUS_METERS = 50


class Campus(abstract.DashboardModel):
    """Spatial boundary for a Location or a user's Pin.

    Two kinds of Campus rows exist:

    Location default (pin=None, profile=None):
        One per Location, describing its canonical boundary.  Set automatically
        from external boundary APIs and editable via the community wiki.
        Enforced by campus_unique_location_default.

    Pin boundary (pin=<Pin>):
        One per Pin, owned by pin.profile.  Replaces the location default for
        that pin's map display.  Keyed by pin so boundaries survive reassigning
        pin.location to a different Location.
        Enforced by campus_unique_pin.

    Each row stores two separate polygon layers:
        generated_polygon: cached result from the boundary API chain (Overpass,
            Regrid, Overture, etc.).  Written once on first access, never
            overwritten by user actions.  Survives the user clearing their custom
            drawing so we avoid a repeat API call on next load.
        polygon: user-drawn boundary.  None means "display generated_polygon".

    effective_polygon returns polygon → generated_polygon → circle fallback.
    Use CampusManager.effective_for_pin(pin) or effective_for(location) to
    resolve the correct Campus for display.
    """

    # User-drawn boundary (clearable). None = fall back to generated_polygon.
    polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # API-fetched boundary (cached). Written on first generate, never cleared by users.
    generated_polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # Radius (metres) used when both polygons are None (circle fallback).
    default_radius_meters = IntegerField(default=_DEFAULT_RADIUS_METERS)

    # The place whose boundary this Campus describes.  Required on all rows.
    # For pin campuses: always matches pin.location (synced lazily by controller).
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="campuses",
    )
    # Set for pin-scoped boundaries; None for location wiki/default boundaries.
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="campus",
    )
    # Mirrors pin.profile for fast profile-based queries without joining through pin.
    # Always None on location default campuses.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="campuses",
    )
    
    objects = CampusManager()

    if TYPE_CHECKING:
        pin_id: int | None
        profile_id: int | None
        location_id: int

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def is_default(self) -> bool:
        """True if this is the location-level default (no profile, no pin)."""
        return self.profile_id is None and self.pin_id is None

    @property
    def effective_polygon(self):
        """User-drawn polygon, API-generated fallback, or a circle from location coords.

        Requires self.location to be loaded (use select_related("location")).
        The circle buffers in degrees (WGS84) - adequate for map display.
        """
        if self.polygon:
            return self.polygon
        if self.generated_polygon:
            return self.generated_polygon
        lat = float(self.location.latitude)
        lon = float(self.location.longitude)
        center = Point(lon, lat, srid=4326)
        radius_deg = self.default_radius_meters / 111_000
        return center.buffer(radius_deg)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        if self.pin_id:
            return f"Campus(pin={self.pin_id}, profile={self.profile_id})"
        owner = f"profile {self.profile_id}" if self.profile_id else "default"
        return f"Campus(location={self.location_id}, {owner})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_campuses"
        get_latest_by = "updated"
        constraints = [
            # One wiki/default boundary per location.
            UniqueConstraint(
                fields=["location"],
                condition=Q(profile__isnull=True, pin__isnull=True),
                name="campus_unique_location_default",
            ),
            # One boundary per pin (pin already encodes location + profile).
            UniqueConstraint(
                fields=["pin"],
                condition=Q(pin__isnull=False),
                name="campus_unique_pin",
            ),
        ]
