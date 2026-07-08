"""Campus model - defines the spatial region for a Wiki or Pin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import MultiPolygonField
from django.contrib.gis.geos import Point
from django.db.models import CASCADE, SET_NULL, ForeignKey, IntegerField, Q
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.campus.queryset import CampusManager

logger = logging.getLogger(__name__)

_DEFAULT_RADIUS_METERS = 50


class Campus(abstract.DashboardModel):
    """Spatial boundary for a community wiki or a user's Pin.

    Two kinds of Campus rows exist:

    Wiki default (pin=None, profile=None):
        One per Wiki, describing its canonical boundary. Set automatically from
        external boundary APIs and editable via the community wiki. Keyed by
        ``wiki`` so the boundary survives when the wiki is repointed to a new
        Location after a coordinate edit.

    Pin boundary (pin=<Pin>):
        One per Pin, owned by pin.profile. Replaces the wiki default for that
        pin's map display. Keyed by pin so boundaries survive reassigning
        pin.location to a different Location.

    Each row stores two separate polygon layers:
        generated_polygon: cached result from the boundary API chain (Overpass,
            Regrid, Overture, etc.). Written once on first access, never
            overwritten by user actions.
        polygon: user-drawn boundary. None means "display generated_polygon".

    effective_polygon returns polygon → generated_polygon → circle fallback.
    Use CampusManager.effective_for_wiki(wiki) or effective_for_pin(pin) to
    resolve the correct Campus for display.
    """

    # User-drawn boundary (clearable). None = fall back to generated_polygon.
    polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # API-fetched boundary (cached). Written on first generate, never cleared by users.
    generated_polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # Radius (metres) used when both polygons are None (circle fallback).
    default_radius_meters = IntegerField(default=_DEFAULT_RADIUS_METERS)

    # TODO [UL-351]: We previously handled Pin->Location relationships differently. Now that they are
    # fully independent, these Campus relationships should be changed.

    # When a campus is linked to a wiki. None for pin-scoped boundaries.
    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="campuses",
    )
    # Legacy FK kept for pin-scoped rows and transitional reads; wiki defaults
    # should be resolved through ``wiki`` instead.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="campuses",
    )
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
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="campuses",
    )

    objects = CampusManager()

    if TYPE_CHECKING:
        wiki_id: int | None
        pin_id: int | None
        profile_id: int | None
        location_id: int | None

    @property
    def is_default(self) -> bool:
        """True if this is the wiki-level default (no profile, no pin)."""
        return self.profile_id is None and self.pin_id is None

    @property
    def coordinate_location(self):
        """Location whose coordinates anchor the circle fallback."""
        if self.location_id:
            return self.location
        if self.pin_id and self.pin is not None and self.pin.location_id:
            return self.pin.location
        if self.wiki_id and self.wiki is not None and self.wiki.location_id:
            return self.wiki.location
        return None

    @property
    def effective_polygon(self):
        """User-drawn polygon, API-generated fallback, or a circle from coords."""
        if self.polygon:
            return self.polygon
        if self.generated_polygon:
            return self.generated_polygon
        location = self.coordinate_location
        if location is None or location.latitude is None or location.longitude is None:
            return None
        lat = float(location.latitude)
        lon = float(location.longitude)
        center = Point(lon, lat, srid=4326)
        radius_deg = self.default_radius_meters / 111_000
        return center.buffer(radius_deg)

    def __str__(self) -> str:
        if self.pin_id:
            return f"Campus(pin={self.pin_id}, profile={self.profile_id})"
        owner = f"profile {self.profile_id}" if self.profile_id else "default"
        if self.wiki_id:
            return f"Campus(wiki={self.wiki_id}, {owner})"
        return f"Campus(location={self.location_id}, {owner})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_campuses"
        get_latest_by = "updated"
        constraints = [
            UniqueConstraint(
                fields=["wiki"],
                condition=Q(profile__isnull=True, pin__isnull=True),
                name="campus_unique_wiki_default",
            ),
            UniqueConstraint(
                fields=["pin"],
                condition=Q(pin__isnull=False),
                name="campus_unique_pin",
            ),
        ]
