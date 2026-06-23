"""Location model - shared, globally recognised data about a physical place."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from django.contrib.gis.db.models import PointField, PolygonField
from django.contrib.gis.geos import Point, Polygon
from django.db.models import Index, ManyToManyField, UUIDField
from django.db.models.fields import CharField, DateField, DecimalField, SlugField, TextField
from django.utils.text import slugify

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


class Location(abstract.AddressableMixin, abstract.Model):
    """Shared, globally recognised data about a physical place.

    Location is the *global* half of the two-model design:
    - Location  - one row per real-world place, shared across all users.
    - Pin       - one row per (user, place) pair; links to a Location via FK.

    A Location is never user-specific. Many users can each have a Pin that
    points at the same Location. The Location stores the canonical name,
    coordinates, address components (via AddressableMixin), Google Maps CID,
    and any other data that is the same regardless of who is looking.

    What does NOT belong here:
    - Custom labels or notes a user gave the place → Pin.nickname / Pin.description
    - Visit history or visit status → Pin.last_visited / Pin.status
    - Per-user coordinate overrides → Pin.latitude / Pin.longitude
    - Priority rankings → Pin.priority
    - User reviews → Review model (FK to Pin, not Location)

    Address fields (street_number, route, locality, etc.) are inherited from
    AddressableMixin and accessed via the state/city/county/address properties
    defined there.  Pin does NOT inherit AddressableMixin; it proxies those
    same properties through its location FK.
    """

    # Public-facing identifier. Non-sequential so users cannot infer location counts
    # or enumerate other locations from a known URL.
    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    # URL slug - globally unique. Auto-generated from name on first save.
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)

    # Canonical name of the place - NOT a user's personal label (that's Pin.nickname).
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)
    # Authoritative coordinates for this place. Pin may override these per-user.
    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    point = PointField(geography=True, default=Point(0, 0))

    # Bounding box for this location. Used to auto-link new pins whose coordinates
    # fall within this polygon. Defaults to a small circle (~50 m) around the point.
    # Users can expand this to cover a campus or multi-building site via the wiki.
    bounding_box = PolygonField(geography=True, null=True, blank=True, srid=4326)

    # Google Maps CID - unsigned 64-bit identifier embedded in place URLs.
    # Stored as Decimal to handle values above signed int64 range (> 2^63-1).
    # Used to de-duplicate Location rows on import and to look up Places API data.
    cid = DecimalField(max_digits=20, decimal_places=0, null=True, blank=True, unique=True)

    # Security indicators: how prevalent each security feature is at this place.
    fences = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    alarms = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    cameras = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    security = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    signs = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    vps = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    plywood = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    locked = CharField(max_length=20, choices=SecurityLevel.choices, default=SecurityLevel.UNKNOWN)
    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    # Shared taxonomy - these represent the real-world place's type, not a user's classification.
    # Users apply their own categories/tags via the Pin's M2M fields.
    categories = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="categorized_locations",
        limit_choices_to={"kind": "category"},
    )
    tags = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="locations",
        limit_choices_to={"kind": "tag"},
    )
    statuses = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="status_locations",
        limit_choices_to={"kind": "status"},
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

    @property
    def place_name(self) -> str | None:
        if self.cached_place_name:
            return self.cached_place_name
        return self.get_place_name()

    def get_place_name(self) -> str | None:
        """Fetch the canonical place name from Google and cache it."""
        if self.latitude is None or self.longitude is None or not (-90 <= float(self.latitude) <= 90) or not (-180 <= float(self.longitude) <= 180):
            return "No Information Available"
        try:
            result = GoogleGeocodingGateway(api_key=settings.google_maps_api_key).get_place_name(
                self.latitude,
                self.longitude,
            )
        except Exception as exc:
            logger.debug("Google place-name lookup failed for location %s: %s", self.pk or self.name, exc)
            result = None
        if not result:
            result = "No Information Available"
        if not self.cached_place_name:
            self.cached_place_name = result
            if self.pk:
                # Use update() to persist without triggering post_save signals
                Location.objects.filter(pk=self.pk).update(cached_place_name=result)
        return result

    def has_place_name(self) -> bool:
        name = self.place_name
        return bool(name) and name != "No Information Available"

    def change_category(self, category_id: int) -> None:
        from urbanlens.dashboard.models.badges.model import Badge

        category = Badge.objects.get(id=category_id, kind="category")
        self.categories.clear()
        self.categories.add(category)
        self.save()

    def suggest_category(self, append_suggestion: bool = False) -> str | None:
        from urbanlens.dashboard.services.ai.factory import get_gateway

        instructions = (
            "Look at the following information about a location and determine what category it belongs in. Example categories are: "
            "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, Firehouse, Fire Tower, "
            "Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, Library, Lighthouse, Mall, Mansion, Military Base, "
            "Monument, Police Station, Power Plant, Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel. "
            "If the location does not fit into any of these categories, provide a new category that is broad enough to include a variety "
            "of similar urbex locations. Do not answer with the name of the location; always answer with a category, like this: <ANSWER>Factory</ANSWER>."
        )

        prompt = ""
        if self.address:
            prompt += f"address: {self.address}\n"
        if self.cached_place_name and self.has_place_name():
            prompt += f"google maps description: {self.cached_place_name}\n"
            instructions += "\nThe google maps description may be helpful, but it also may be inaccurate. Use your best judgement.\n"
        if self.name:
            prompt += f"location title: {self.name}\n"
        if self.description:
            prompt += f"description: {self.description}\n"

        if not prompt:
            return None

        gateway = get_gateway("category_suggestions", instructions=instructions)
        if not gateway:
            return None
        category_name = gateway.send_prompt(prompt)
        if not category_name or len(category_name) < 3:
            return None

        if append_suggestion:
            self.add_category(category_name, save=False)
        return category_name

    def add_category(self, category_name: str, save: bool = True) -> Badge | None:
        from urbanlens.dashboard.models.badges.model import Badge

        category_name = category_name.lower()
        try:
            category, _created = Badge.objects.get_or_create(name=category_name, kind="category", defaults={"profile": None})
            if category:
                self.categories.add(category)
                if save:
                    self.save()
                return category
        except Exception as e:
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

    def _generate_slug(self) -> str:
        """Derive a slug that is globally unique across all locations."""
        base = slugify(self.name or "location")[:255] or "location"
        candidate = base
        n = 2
        qs = Location.objects.all()
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        while qs.filter(slug=candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = self._generate_slug()
        if self.latitude is not None and self.longitude is not None:
            self.point = Point(float(self.longitude), float(self.latitude), srid=4326)
            if self.bounding_box is None:
                bbox = self.point.buffer(_DEFAULT_BBOX_DEGREES)
                if isinstance(bbox, Polygon):
                    bbox.srid = 4326
                    self.bounding_box = bbox
                else:
                    logger.warning(
                        "Failed to create bounding box for location %s (%s, %s)",
                        self.name,
                        self.latitude,
                        self.longitude,
                    )
        super().save(*args, **kwargs)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"]),
            Index(fields=["latitude", "longitude"]),
            Index(fields=["name"]),
            Index(fields=["cid"]),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
