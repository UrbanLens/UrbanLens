"""Route model - a recorded GPS track/route imported from GPX or Google Takeout."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.contrib.gis.db.models import LineStringField
from django.db.models import (
    CASCADE,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKey,
    Index,
    IntegerField,
    TextChoices,
    UUIDField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.routes.queryset import RouteManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


class RouteSource(TextChoices):
    """Origin of a Route record.

    - GPX_TRACK: A ``<trk>`` recording imported from a GPX file.
    - GPX_ROUTE: A ``<rte>`` planned route imported from a GPX file.
    - GOOGLE_TAKEOUT_SEMANTIC: An ``activitySegment`` imported from Google
      Takeout Semantic Location History.
    """

    GPX_TRACK = "gpx_track", "GPX Track"
    GPX_ROUTE = "gpx_route", "GPX Route"
    GOOGLE_TAKEOUT_SEMANTIC = "google_takeout_semantic", "Google Takeout (Semantic History)"


class Route(abstract.Model):
    """A recorded path a profile travelled, imported from GPX or Google Takeout.

    The stored ``path`` is a simplified polyline (see
    ``services.import_formats.route_geometry.simplify_and_measure``) - raw GPS
    points are not retained after import. ``distance_meters`` is computed from
    the raw points before simplification, so it remains accurate regardless of
    how aggressively the display geometry was simplified.

    Unlike Pin/Location, routes are personal GPS data with no shared/wiki
    analog, so they are always scoped to the owning profile - there is no
    ``is_private`` flag.

    Attributes:
        profile: Owning profile - routes are never shared between profiles.
        name: Route label, from the source file's track/route name or filename.
        source: Which import pipeline produced this route.
        source_filename: Original uploaded filename, kept for reference.
        path: Simplified route geometry (WGS84 line string).
        raw_point_count: Number of points before simplification.
        simplified_point_count: Number of points after simplification.
        distance_meters: Cumulative geodesic distance over the raw points.
        elevation_gain_meters: Total ascent, when the source data has elevation.
        elevation_loss_meters: Total descent, when the source data has elevation.
        started_at: Timestamp of the first point that carried one, if any.
        ended_at: Timestamp of the last point that carried one, if any.
    """

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="routes",
    )
    name = CharField(max_length=255, blank=True, default="")
    source = CharField(max_length=30, choices=RouteSource.choices)
    source_filename = CharField(max_length=255, blank=True, default="")

    path = LineStringField(geography=True, srid=4326)
    raw_point_count = IntegerField(default=0)
    simplified_point_count = IntegerField(default=0)

    distance_meters = FloatField(default=0.0)
    elevation_gain_meters = FloatField(null=True, blank=True)
    elevation_loss_meters = FloatField(null=True, blank=True)

    started_at = DateTimeField(null=True, blank=True)
    ended_at = DateTimeField(null=True, blank=True)

    objects = RouteManager()

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this route.

        Returns:
            The route's name, or a fallback describing its source and date.
        """
        if self.name:
            return self.name
        when = self.started_at.strftime("%Y-%m-%d") if self.started_at else "unknown date"
        return f"{self.get_source_display()} on {when}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_routes"
        ordering = ["-started_at", "-created"]
        get_latest_by = "started_at"
        indexes = [
            Index(fields=["uuid"], name="dashboard_route_uuid_idx"),
            Index(fields=["profile"], name="dashboard_route_profile_idx"),
            Index(fields=["profile", "started_at"], name="dashboard_route_profile_started_idx"),
        ]
