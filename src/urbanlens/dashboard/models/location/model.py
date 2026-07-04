"""Location model - shared, globally recognised data about a physical place."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import PointField, PolygonField
from django.contrib.gis.geos import Point
from django.db import DatabaseError
from django.db.models import Index, ManyToManyField
from django.db.models.fields import CharField, DateField, DecimalField, SlugField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.location.queryset import LocationManager
from urbanlens.dashboard.services.locations.boundaries import default_bbox

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.model import Badge


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

    # Global uniqueness: each place has one canonical slug across all users.
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)

    # Canonical name of the place - NOT a user's personal label (that's Pin.name).
    name = CharField(max_length=255)
    # External-source name for this location. User edits must never write this field.
    official_name = CharField(max_length=255, null=True, blank=True)
    description = TextField(null=True, blank=True)

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    # Shared taxonomy - represents the real-world place's type, visible to all users.
    badges = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="locations",
    )

    objects = LocationManager()

    def get_unique_search_name(self, *, include_country: bool = True) -> str | None:
        """Name to use when searching for this location in external APIs."""
        name = self.official_name
        if not name:
            return None

        parts = [name]
        if self.address_basic and self.address_basic != name:
            parts.append(self.address_basic)

        if self.city:
            parts.append(self.city)
        elif self.county:
            parts.append(self.county)
        if self.state:
            parts.append(self.state)
        if include_country and self.country:
            parts.append(self.country)
        return " ".join(parts)

    @property
    def categories(self):
        """Badges of kind "category" attached to this location."""
        return self.badges.all().categories()

    @property
    def tags(self):
        """Badges of kind "tag" attached to this location."""
        return self.badges.all().tags()

    @property
    def statuses(self):
        """Badges of kind "status" attached to this location."""
        return self.badges.all().statuses()

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
            "official_name": self.official_name,
            "place_name": self.place_name,
            "description": self.description,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    def _slugify_base(self) -> str:
        return self.name or "location"

    def save(self, *args, **kwargs) -> None:
        """Auto-generate derived geographic fields before saving."""
        if not self.slug:
            self.slug = self._generate_slug()
        if self.latitude is not None and self.longitude is not None:
            lon = float(self.longitude)
            lat = float(self.latitude)
            self.point = Point(lon, lat, srid=4326)
        super().save(*args, **kwargs)

    class Meta(abstract.AddressableModel.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"]),
            Index(fields=["latitude", "longitude"]),
            Index(fields=["name"]),
            Index(fields=["official_name"]),
            Index(fields=["google_place"]),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
