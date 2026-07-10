"""Boundary model - typed spatial regions for Locations, Wikis, and Pins."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import MultiPolygonField
from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, IntegerField, Q, TextChoices
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS, BoundaryManager, circle_for_coordinates

logger = logging.getLogger(__name__)


class BoundaryType(TextChoices):
    """The kind of physical region a boundary describes.

    PROPERTY is the parcel/grounds of a place (a campus, a lot). BUILDING is a
    single structure's footprint. When an external source is ambiguous about
    which it provides, treat it as PROPERTY.
    """

    PROPERTY = "property", "Property"
    BUILDING = "building", "Building"


class Boundary(abstract.DashboardModel):
    """A typed spatial boundary (property or building) for a place.

    Three kinds of Boundary rows exist, distinguished by which FK is set:

    Location default (location=<Location>, pin=None, wiki=None, profile=None):
        Shared, API-generated geometry for a physical place. One per
        (location, boundary_type). ``generated_polygon`` is filled lazily by
        the boundary provider chain; ``generated_at`` marks that the chain ran
        (even when it found nothing). These rows are the only ones used for
        point→location matching, and only via ``generated_polygon`` so a
        user-drawn shape can never inflate a location's match area.

    Wiki boundary (wiki=<Wiki>, pin=None):
        Community-drawn customization made on the wiki page. Overrides the
        location default for display. Keyed by wiki so it survives the wiki
        being repointed to a new Location after a coordinate edit.

    Pin boundary (pin=<Pin>, profile=pin.profile):
        A user's personal customization made on the pin detail page. Overrides
        everything else for that pin's map display.

    When no property boundary exists at all, the effective boundary is a
    circle of ``default_radius_meters`` around the location's coordinates.
    Building boundaries have no such fallback - absence means "no known
    building here".

    Use ``Boundary.objects.effective_polygon_for_pin`` /
    ``effective_polygon_for_wiki`` to resolve display geometry, including
    detail-pin inheritance rules.
    """

    boundary_type = CharField(max_length=20, choices=BoundaryType.choices, default=BoundaryType.PROPERTY)

    # User-drawn boundary (clearable). None = fall back to generated_polygon.
    polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # API-fetched boundary (cached). Written by the generation task, never cleared by users.
    generated_polygon = MultiPolygonField(geography=True, srid=4326, null=True, blank=True)
    # When the provider chain last ran for this row. A non-null value with a
    # null generated_polygon means "we looked and found nothing" - don't refetch
    # on every page view.
    generated_at = DateTimeField(null=True, blank=True)
    # Radius (metres) used for the property-circle fallback when no polygon exists.
    default_radius_meters = IntegerField(default=DEFAULT_RADIUS_METERS)

    # The physical place this boundary belongs to. Set on location-default rows;
    # mirrors pin.location / wiki.location on customized rows for the circle anchor.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="boundaries",
    )
    # Community customization keyed by wiki. None for location defaults and pin rows.
    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="boundaries",
    )
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="boundaries",
    )
    # Mirrors pin.profile for fast profile-based queries without joining through pin.
    # Always None on location defaults and wiki rows.
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="boundaries",
    )

    objects = BoundaryManager()

    if TYPE_CHECKING:
        wiki_id: int | None
        pin_id: int | None
        profile_id: int | None
        location_id: int | None

    @property
    def is_location_default(self) -> bool:
        """True if this is the shared location-default row (no pin, wiki, or profile)."""
        return self.pin_id is None and self.wiki_id is None and self.profile_id is None

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
    def drawn_or_generated_polygon(self):
        """User/community-drawn polygon, else the API-generated one, else None.

        Unlike ``effective_polygon`` this never synthesizes a circle, so it is
        safe for resolution chains that must distinguish "has real geometry"
        from "needs a fallback".
        """
        return self.polygon or self.generated_polygon

    @property
    def effective_polygon(self):
        """Drawn polygon, generated fallback, or (property only) a circle from coords."""
        if polygon := self.drawn_or_generated_polygon:
            return polygon
        if self.boundary_type != BoundaryType.PROPERTY:
            return None
        location = self.coordinate_location
        if location is None:
            return None
        return circle_for_coordinates(location.latitude, location.longitude, self.default_radius_meters)

    def __str__(self) -> str:
        if self.pin_id:
            owner = f"pin={self.pin_id}, profile={self.profile_id}"
        elif self.wiki_id:
            owner = f"wiki={self.wiki_id}"
        else:
            owner = f"location={self.location_id}"
        return f"Boundary({self.boundary_type}, {owner})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_boundaries"
        get_latest_by = "updated"
        verbose_name_plural = "boundaries"
        constraints = [
            UniqueConstraint(
                fields=["location", "boundary_type"],
                condition=Q(pin__isnull=True, wiki__isnull=True, profile__isnull=True),
                name="boundary_unique_location_default",
            ),
            UniqueConstraint(
                fields=["wiki", "boundary_type"],
                condition=Q(wiki__isnull=False, pin__isnull=True),
                name="boundary_unique_wiki",
            ),
            UniqueConstraint(
                fields=["pin", "boundary_type"],
                condition=Q(pin__isnull=False),
                name="boundary_unique_pin",
            ),
        ]
