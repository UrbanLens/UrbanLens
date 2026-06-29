"""Location model - shared, globally recognised data about a physical place."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.contrib.gis.db.models import PointField, PolygonField
from django.contrib.gis.geos import Point, Polygon
from django.db import DatabaseError
from django.db.models import Index, ManyToManyField, UUIDField
from django.db.models.fields import CharField, DateField, DecimalField, SlugField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.location.queryset import LocationManager
from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.model import Badge


# ~50 m radius expressed in degrees (at mid-latitudes). Used as the default
# bounding box when a new Location is created without an explicit boundary.
_DEFAULT_BBOX_DEGREES = 0.00045

logger = logging.getLogger(__name__)


class Location(abstract.HasSlug, abstract.SecurityModel, abstract.AddressableModel):
    """Shared, globally recognised data about a physical place.

    Location is the *global* half of the two-model design:
    - Location  - one row per real-world place, shared across all users.
    - Pin       - one row per (user, place) pair; links to a Location via FK.

    A Location is never user-specific. Many users can each have a Pin that
    points at the same Location. The Location stores the canonical name,
    coordinates, address components (via AddressableMixin), Google Maps CID,
    and any other data that is the same regardless of who is looking.

    What does NOT belong here:
    - Custom labels or notes a user gave the place → Pin.name / Pin.description
    - Visit history or visit status → Pin.last_visited / Pin.status
    - Per-user coordinate overrides → Pin.latitude / Pin.longitude
    - Priority rankings → Pin.priority
    - User reviews → Review model (FK to Pin, not Location)

    Address fields (street_number, route, locality, etc.) are inherited from
    AddressableMixin and accessed via the state/city/county/address properties
    defined there.
    """

    # Canonical name of the place - NOT a user's personal label (that's Pin.name).
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)

    # Bounding box for this location. Used to auto-link new pins whose coordinates
    # fall within this polygon. Defaults to a small circle (~50 m) around the point.
    # Users can expand this to cover a campus or multi-building site via the wiki.
    bounding_box = PolygonField(geography=True, null=True, blank=True, srid=4326)

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    # Shared taxonomy - represents the real-world place's type, visible to all users.
    badges = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="locations",
    )

    objects = LocationManager()

    @property
    def effective_date_last_active(self):
        """Date the place was last active, inferred from date_abandoned if not set explicitly."""
        from datetime import timedelta

        if self.date_last_active is not None:
            return self.date_last_active
        if self.date_abandoned is not None:
            return self.date_abandoned - timedelta(days=1)
        return None

    def add_category(self, category_name: str, save: bool = True) -> Badge | None:
        from urbanlens.dashboard.models.badges.model import Badge

        category_name = category_name.lower()
        try:
            category, _created = Badge.objects.get_or_create(name=category_name, kind="category", defaults={"profile": None})
            if category:
                self.badges.add(category)
                if save:
                    self.save()
                return category
        except DatabaseError as e:
            logger.exception("failed to add category %s to location -> %s", category_name, e)
        return None

    def __str__(self):
        return self.name or f"Location({self.pk})"

    def to_json(self) -> dict:
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            "id": self.id,
            "name": self.name,
            "place_name": self.place_name,
            "description": self.description,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    def save(self, *args, **kwargs) -> None:
        """Auto-generate derived geographic fields before saving."""
        if not self.slug:
            self.slug = self._generate_slug()
        if self.latitude is not None and self.longitude is not None:
            lon = float(self.longitude)
            lat = float(self.latitude)
            self.point = Point(lon, lat, srid=4326)
            if self.bounding_box is None:
                self.bounding_box = Polygon.from_bbox(
                    (
                        lon - _DEFAULT_BBOX_DEGREES,
                        lat - _DEFAULT_BBOX_DEGREES,
                        lon + _DEFAULT_BBOX_DEGREES,
                        lat + _DEFAULT_BBOX_DEGREES,
                    ),
                )
        super().save(*args, **kwargs)

    class Meta(abstract.AddressableModel.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"]),
            Index(fields=["latitude", "longitude"]),
            Index(fields=["name"]),
            Index(fields=["google_place"]),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
