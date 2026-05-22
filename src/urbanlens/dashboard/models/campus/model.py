"""Campus model - defines the spatial region for a Location."""

from __future__ import annotations

import logging

from django.contrib.gis.db.models import PolygonField
from django.contrib.gis.geos import Point
from django.db.models import CASCADE, ForeignKey, IntegerField, Q
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.campus.queryset import CampusManager

logger = logging.getLogger(__name__)

_DEFAULT_RADIUS_METERS = 50


class Campus(abstract.Model):
    """Spatial region for a Location, optionally scoped to a specific user.

    Campus defines *how much of the map* a Location occupies - its boundary
    polygon.  It is a separate concern from:
    - Location: canonical address, coordinates, and Google Maps metadata.
    - Pin: a user's personal record for visiting or tracking a place.

    Two kinds of Campus rows exist:
    - Admin default (profile=None): one per Location, set by an admin to define
      the canonical boundary polygon.  Enforced by campus_unique_default_location.
    - User override (profile=<Profile>): one per (Location, Profile) pair.
      Visible only to that user; replaces the admin default for display purposes.
      Enforced by campus_unique_user_location.

    If no Campus row exists for a Location at all, callers should fall back to a
    generated circle centred on Location.latitude / Location.longitude.
    Use CampusManager.effective_for(location, profile) to resolve this chain.
    """

    # The place whose boundary this Campus describes.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="campuses",
    )
    # None → admin-defined default, visible to all users who have no personal override.
    # Set → personal override, visible only to this profile.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="campuses",
    )
    # The region boundary polygon.  None means "generate a circle fallback"
    # (see effective_polygon).  Admin can leave this null when first creating the
    # default Campus and rely on default_radius_meters.
    polygon = PolygonField(geography=True, srid=4326, null=True, blank=True)
    # Radius (metres) used to generate the circle when polygon is None.
    # Irrelevant when polygon is set.
    default_radius_meters = IntegerField(default=_DEFAULT_RADIUS_METERS)

    objects = CampusManager()

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def is_default(self) -> bool:
        """True if this is the admin-defined default (profile=None)."""
        return self.profile_id is None

    @property
    def effective_polygon(self):
        """The stored polygon, or a generated circle if polygon is None.

        The circle buffers in degrees (WGS84), which is approximate - it
        slightly distorts shape at high latitudes.  Adequate for map display;
        for metric-accurate buffering use PostGIS ST_Buffer on the server side.

        Requires self.location to be loaded (use select_related("location")).
        """
        if self.polygon:
            return self.polygon
        lat = float(self.location.latitude)
        lon = float(self.location.longitude)
        center = Point(lon, lat, srid=4326)
        # 1 degree latitude ≈ 111 km; this approximation is sufficient for display.
        radius_deg = self.default_radius_meters / 111_000
        return center.buffer(radius_deg)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        owner = f"profile {self.profile_id}" if self.profile_id else "default"
        return f"Campus(location={self.location_id}, {owner})"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_campuses"
        get_latest_by = "updated"
        constraints = [
            # One personal campus per (user, location) pair.
            UniqueConstraint(
                fields=["location", "profile"],
                condition=Q(profile__isnull=False),
                name="campus_unique_user_location",
            ),
            # One admin-default campus per location (partial index avoids NULL != NULL pitfall).
            UniqueConstraint(
                fields=["location"],
                condition=Q(profile__isnull=True),
                name="campus_unique_default_location",
            ),
        ]
