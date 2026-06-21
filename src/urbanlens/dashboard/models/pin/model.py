"""Pin model - a user's personal record for a location."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import (
    CASCADE,
    SET_NULL,
    ForeignKey,
    ImageField,
    Index,
    ManyToManyField,
    Q,
    UniqueConstraint,
    UUIDField,
)
from django.db.models.fields import CharField, DateField, DateTimeField, DecimalField, IntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import SecurityLevel, TextChoices
from urbanlens.dashboard.models.pin.queryset import PinManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.reviews import Manager as ReviewManager

logger = logging.getLogger(__name__)


class PinType(TextChoices):
    LOCATION_MARKER = "location", "Location"
    BUILDING = "building", "Building"
    ENTRANCE = "entrance", "Entrance"
    POINT_OF_INTEREST = "poi", "Point of Interest"
    DANGER = "danger", "Danger"
    OTHER = "other", "Other"


class Pin(abstract.Model):
    """A user's personal record for a physical location.

    Pin is the *personal* half of the two-model design:
    - Location  - one row per real-world place, shared across all users.
    - Pin       - one row per (user, place) pair; links to a Location via FK.

    A Pin belongs to exactly one Profile (user). Multiple users can each have
    their own Pin that references the same Location. Everything stored here is
    specific to that one user: their custom label, notes, visit history, status,
    priority, and an optional coordinate override to reposition the marker.

    What does NOT belong here:
    - Canonical address components (street, city, state …) → Location / AddressableMixin
    - Authoritative coordinates → Location.latitude / Location.longitude
    - Google Maps CID or cached place name → Location
    - Place-level categories shared across users → Location.categories

    Nullable override fields - None means "inherit from Location":
    - name      → falls back to location.name       (use effective_name)
    - latitude  → falls back to location.latitude   (use effective_latitude)
    - longitude → falls back to location.longitude  (use effective_longitude)

    Address and place metadata are accessed through proxy properties defined
    below that delegate to self.location.  Do not add address fields directly
    to Pin - they live on AddressableMixin which only Location inherits.
    """

    # Public-facing identifier. Non-sequential so users cannot enumerate other pins.
    uuid = UUIDField(default=uuid4, unique=True, editable=False)

    # User's custom label. None = show location.name instead (see effective_name).
    # Do NOT store canonical place names here - those belong on Location.
    nickname = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=255, null=True, blank=True)
    # User's personal notes. Unrelated to Location.description (place-level info).
    description = TextField(null=True, blank=True)
    priority = IntegerField(default=0)
    vulnerability = IntegerField(default=0)
    last_visited = DateTimeField(null=True, blank=True)
    # Per-user coordinate override. None = use location.latitude/longitude (see effective_latitude/longitude).
    # Only set these when the user wants to reposition the marker from the canonical Location coords.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    custom_icon = ImageField(upload_to="pin_custom_icons/", null=True, blank=True)
    pin_type = CharField(choices=PinType.choices, default=PinType.LOCATION_MARKER, max_length=30)
    point = PointField(geography=True, default=Point(0, 0))

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="pins",
    )
    # The shared place this pin points at. SET_NULL so deleting a Location
    # doesn't cascade-delete all users' Pins for that place.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pins",
    )
    categories = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="categorized_pins",
        limit_choices_to={"kind": "category"},
    )
    tags = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="pins",
        limit_choices_to={"kind": "tag"},
    )
    statuses = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="status_pins",
        limit_choices_to={"kind": "status"},
    )
    # Direct hex color override for this pin (e.g. "#F44336"). Used by detail pins
    # when the user explicitly picks a color in the dialog.
    color = CharField(max_length=20, null=True, blank=True)

    # Detail-pin circle styling: background fill and border around the icon.
    # Opacity stored as 0-100 integer (percent).
    detail_bg_color = CharField(max_length=20, null=True, blank=True)
    detail_bg_opacity = IntegerField(default=80)
    detail_border_color = CharField(max_length=20, null=True, blank=True)
    detail_border_opacity = IntegerField(default=100)

    # Security indicators: how prevalent each security feature is, per this user's observation.
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

    # Self-referential FK for personal detail pins (private to pin owner).
    parent_pin = ForeignKey(
        "self",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="detail_pins",
    )
    # Community detail pin - attached directly to a Location (wiki-level, shared).
    parent_location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="location_detail_pins",
    )

    if TYPE_CHECKING:
        profile_id: int
        location_id: int | None
        reviews: ReviewManager

    objects = PinManager()

    # ------------------------------------------------------------------
    # Effective values - resolve overrides against the linked Location
    # ------------------------------------------------------------------

    @property
    def effective_icon(self) -> str | None:
        """Icon to display for this pin following the priority chain.

        Priority:
        1. custom_icon uploaded directly for this pin (returns URL)
        2. standard icon key selected for this pin
        3. highest-order tag that has any icon (custom_icon beats icon)
        4. None - caller should fall back to location category or default marker

        Prefetch tags when calling in bulk (e.g. get_map_data).
        """
        if self.custom_icon:
            return self.custom_icon.url
        if self.icon:
            return self.icon
        for tag in self.tags.order_by("-order"):
            if tag.custom_icon:
                return tag.custom_icon.url
            if tag.icon:
                return tag.icon
        return None

    @property
    def effective_color(self) -> str | None:
        """Color hex string for this pin, inherited from the highest-order tag with a color.

        Prefetch tags when calling in bulk (e.g. get_map_data).
        """
        for tag in self.tags.order_by("-order"):
            if tag.color:
                return tag.color
        return None

    # Names produced by Google Maps when a place has no real identity. A pin
    # whose effective_name is one of these has no useful search query to build.
    _MEANINGLESS_NAMES: frozenset[str] = frozenset({"Dropped pin", "No Information Available", ""})

    @property
    def effective_name(self) -> str:
        """User's custom name, or the location's canonical name."""
        return self.nickname or (self.location.name if self.location else "")

    @property
    def has_meaningful_name(self) -> bool:
        """True when the pin has a real name worth using as a search query."""
        return self.effective_name not in self._MEANINGLESS_NAMES

    @property
    def effective_latitude(self) -> float | None:
        """User's position override, or the location's latitude."""
        if self.latitude is not None:
            return float(self.latitude)
        return float(self.location.latitude) if self.location else None

    @property
    def effective_longitude(self) -> float | None:
        """User's position override, or the location's longitude."""
        if self.longitude is not None:
            return float(self.longitude)
        return float(self.location.longitude) if self.location else None

    @property
    def effective_date_last_active(self):
        """Date the place was last active, inferred from date_abandoned if not set explicitly."""
        from datetime import timedelta

        if self.date_last_active is not None:
            return self.date_last_active
        if self.date_abandoned is not None:
            return self.date_abandoned - timedelta(days=1)
        return None

    # ------------------------------------------------------------------
    # Location proxies
    # Address, place name, and geo metadata all live on the shared Location.
    # These properties are convenience accessors so callers don't need to
    # write `pin.location.city` everywhere - but the data is NOT duplicated
    # on Pin.  Never add address fields directly to this model.
    # ------------------------------------------------------------------

    @property
    def place_name(self) -> str | None:
        return self.location.place_name if self.location else None

    @property
    def address(self) -> str | None:
        return self.location.address if self.location else None

    @property
    def address_basic(self) -> str | None:
        return self.location.address_basic if self.location else None

    @property
    def address_extended(self) -> str | None:
        return self.location.address_extended if self.location else None

    @property
    def state(self) -> str | None:
        return self.location.state if self.location else None

    @property
    def county(self) -> str | None:
        return self.location.county if self.location else None

    @property
    def city(self) -> str | None:
        return self.location.city if self.location else None

    @property
    def country(self) -> str | None:
        return self.location.country if self.location else None

    @property
    def cached_place_name(self) -> str | None:
        return self.location.cached_place_name if self.location else None

    def has_place_name(self) -> bool:
        if not self.location:
            return False
        return self.location.has_place_name()

    # ------------------------------------------------------------------
    # Rating
    # ------------------------------------------------------------------

    @property
    def rating(self) -> int:
        try:
            review = self.reviews.all().latest()
            if review:
                return review.rating
        except ObjectDoesNotExist:
            logger.debug("no rating found for pin %s", self.id)
        return 0

    # ------------------------------------------------------------------
    # Category helpers (personal classification for this pin)
    # ------------------------------------------------------------------

    def change_category(self, category_id: int) -> None:
        from urbanlens.dashboard.models.badges.model import Badge

        category = Badge.objects.get(id=category_id, kind="category")
        self.categories.clear()
        self.categories.add(category)
        self.save()

    def suggest_category(self, append_suggestion: bool = False) -> str | None:
        """Suggest a category using the pin's personal context and location metadata."""
        from urbanlens.dashboard.services.ai.factory import get_gateway
        from urbanlens.dashboard.services.ai.keywords import categorize_by_keywords

        keyword_parts = [p for p in (self.effective_name, self.place_name if self.has_place_name() else None) if p]
        if keyword_parts:
            category_name = categorize_by_keywords(" ".join(keyword_parts))
            if category_name:
                logger.debug("Keyword-matched category '%s' for pin %s", category_name, self.pk)
                if append_suggestion:
                    self.add_category(category_name, save=False)
                return category_name

        instructions = (
            "Look at the following information about a location and determine what category it belongs in. Example categories are: "
            "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, Firehouse, Fire Tower, "
            "Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, Library, Lighthouse, Mall, Mansion, Military Base, "
            "Monument, Police Station, Power Plant, Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel. "
            "If the pin does not fit into any of these categories, provide a new category that is broad enough to include a variety "
            "of similar urbex locations. Do not answer with the name of the pin; always answer with a category, like this: <ANSWER>Factory</ANSWER>."
        )

        prompt = ""
        if self.address:
            prompt += f"address: {self.address}\n"
        if self.has_place_name():
            prompt += f"google maps description: {self.place_name}\n"
            instructions += "\nThe google maps description may be helpful, but it also may be inaccurate. Use your best judgement.\n"
        if self.effective_name:
            prompt += f"location title: {self.effective_name}\n"
        if self.description:
            prompt += f"user notes: {self.description}\n"

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
            category, _ = Badge.objects.get_or_create(name=category_name, kind="category", defaults={"profile": None})
            if category:
                self.categories.add(category)
                if save:
                    self.save()
                return category
        except Exception as e:
            logger.exception("failed to add category %s to pin -> %s", category_name, e)
        return None

    # ------------------------------------------------------------------
    # Serialisation / display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        status_labels = ", ".join(s.name for s in self.statuses.all()) or "None"
        return (
            f"Name: {self.effective_name}\n"
            f"Description: {self.description or ''}\n"
            f"Priority: {self.priority}\n"
            f"Last Visited: {self.last_visited}\n"
            f"Status: {status_labels}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "uuid": str(self.uuid),
            "name": self.effective_name,
            "icon": self.effective_icon,
            "place_name": self.place_name,
            "description": self.description,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "priority": self.priority,
            "last_visited": self.last_visited.isoformat() if self.last_visited else "never",
            "latitude": self.effective_latitude,
            "longitude": self.effective_longitude,
            "statuses": [{"id": s.id, "name": s.name, "color": s.color, "icon": s.icon} for s in self.statuses.all()],
            "profile": self.profile.id,
            "rating": self.rating,
            "color": self.effective_color,
            "tags": [{"id": t.id, "name": t.name, "color": t.color, "icon": t.icon} for t in self.tags.all()],
        }

    def to_detail_json(self) -> dict:
        """Compact serialisation for detail-pin map markers."""
        return {
            "uuid": str(self.uuid),
            "name": self.effective_name,
            "description": self.description or "",
            "pin_type": self.pin_type,
            "latitude": self.effective_latitude,
            "longitude": self.effective_longitude,
            "icon": self.icon or self.effective_icon,
            "color": self.color or self.effective_color,
            "bg_color": self.detail_bg_color or "",
            "bg_opacity": self.detail_bg_opacity,
            "border_color": self.detail_border_color or "",
            "border_opacity": self.detail_border_opacity,
        }

    def save(self, *args, **kwargs) -> None:
        lat = self.effective_latitude
        lon = self.effective_longitude
        if lat is not None and lon is not None:
            self.point = Point(float(lon), float(lat), srid=4326)
        super().save(*args, **kwargs)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_user_pins"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"]),
            Index(fields=["profile"]),
            Index(fields=["profile", "priority"]),
            Index(fields=["profile", "last_visited"]),
            Index(fields=["latitude", "longitude"]),
            Index(fields=["parent_pin"]),
            Index(fields=["parent_location"], name="dashboard_pin_parent_loc_idx"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["latitude", "longitude", "profile"],
                condition=Q(parent_pin__isnull=True, parent_location__isnull=True),
                name="dashboard_pin_unique_location_per_profile",
            ),
        ]


class PinNote(abstract.Model):
    """A private, timestamped note that only the pin owner can see.

    Distinct from Pin.description (single editable blob). Notes are append-only
    entries - the owner can delete individual notes but not edit them in place.
    """

    pin = ForeignKey(
        Pin,
        on_delete=CASCADE,
        related_name="notes",
    )
    text = TextField()

    def __str__(self) -> str:
        return f"[{self.pin_id}] {self.text[:60]}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_pin_notes"
        ordering = ["-created"]
        indexes = [
            Index(fields=["pin"], name="dashboard_pn_pin_idx"),
        ]
