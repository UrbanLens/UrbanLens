"""Pin model - a user's personal record for a location."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.db.models import CASCADE, SET_NULL, ForeignKey, Index, ManyToManyField
from django.db.models.fields import CharField, DateTimeField, DecimalField, IntegerField, TextField
from django.forms import ImageField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.pin.queryset import PinManager

if TYPE_CHECKING:
    from urbanlens.dashboard.models.categories.model import Category
    from urbanlens.dashboard.models.reviews import Manager as ReviewManager

logger = logging.getLogger(__name__)


class PinStatus(TextChoices):
    NOT_VISITED = "not visited"
    VISITED = "visited"
    WISH_TO_VISIT = "wish to visit"
    DEMOLISHED = "demolished"


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

    # The shared place this pin points at. SET_NULL so deleting a Location
    # doesn't cascade-delete all users' Pins for that place.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pins",
    )
    # User's custom label. None = show location.name instead (see effective_name).
    # Do NOT store canonical place names here - those belong on Location.
    nickname = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=255, null=True, blank=True)
    # User's personal notes. Unrelated to Location.description (place-level info).
    description = TextField(null=True, blank=True)
    priority = IntegerField(default=0)
    last_visited = DateTimeField(null=True, blank=True)
    # Per-user coordinate override. None = use location.latitude/longitude (see effective_latitude/longitude).
    # Only set these when the user wants to reposition the marker from the canonical Location coords.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    custom_icon = ImageField()
    status = CharField(choices=PinStatus.choices, default=PinStatus.WISH_TO_VISIT)
    point = PointField(geography=True, default=Point(0, 0))

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="pins",
    )
    categories = ManyToManyField(
        "dashboard.Category",
        blank=True,
        default=list,
    )
    tags = ManyToManyField(
        "dashboard.Tag",
        blank=True,
        default=list,
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
    def effective_name(self) -> str:
        """User's custom name, or the location's canonical name."""
        return self.nickname or (self.location.name if self.location_id else "")

    @property
    def effective_latitude(self) -> float | None:
        """User's position override, or the location's latitude."""
        if self.latitude is not None:
            return float(self.latitude)
        return float(self.location.latitude) if self.location_id else None

    @property
    def effective_longitude(self) -> float | None:
        """User's position override, or the location's longitude."""
        if self.longitude is not None:
            return float(self.longitude)
        return float(self.location.longitude) if self.location_id else None

    # ------------------------------------------------------------------
    # Location proxies
    # Address, place name, and geo metadata all live on the shared Location.
    # These properties are convenience accessors so callers don't need to
    # write `pin.location.city` everywhere - but the data is NOT duplicated
    # on Pin.  Never add address fields directly to this model.
    # ------------------------------------------------------------------

    @property
    def place_name(self) -> str | None:
        return self.location.place_name if self.location_id else None

    @property
    def address(self) -> str | None:
        return self.location.address if self.location_id else None

    @property
    def address_basic(self) -> str | None:
        return self.location.address_basic if self.location_id else None

    @property
    def address_extended(self) -> str | None:
        return self.location.address_extended if self.location_id else None

    @property
    def state(self) -> str | None:
        return self.location.state if self.location_id else None

    @property
    def county(self) -> str | None:
        return self.location.county if self.location_id else None

    @property
    def city(self) -> str | None:
        return self.location.city if self.location_id else None

    @property
    def country(self) -> str | None:
        return self.location.country if self.location_id else None

    @property
    def cached_place_name(self) -> str | None:
        return self.location.cached_place_name if self.location_id else None

    def has_place_name(self) -> bool:
        return self.location.has_place_name() if self.location_id else False

    # ------------------------------------------------------------------
    # Rating
    # ------------------------------------------------------------------

    @property
    def rating(self) -> int:
        try:
            review = self.reviews.all().latest()
            if review:
                return review.rating
        except Exception:
            logger.debug("no rating found for pin %s", self.id)
        return 0

    # ------------------------------------------------------------------
    # Category helpers (personal classification for this pin)
    # ------------------------------------------------------------------

    def change_category(self, category_id: int) -> None:
        from urbanlens.dashboard.models.categories.model import Category

        category = Category.objects.get(id=category_id)
        self.categories.clear()
        self.categories.add(category)
        self.save()

    def suggest_category(self, append_suggestion: bool = False) -> str | None:
        """Suggest a category using the pin's personal context and location metadata."""
        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway
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

        gateway = CloudflareGateway(instructions=instructions)
        category_name = gateway.send_prompt(prompt)
        if not category_name or len(category_name) < 3:
            return None

        if append_suggestion:
            self.add_category(category_name, save=False)
        return category_name

    def add_category(self, category_name: str, save: bool = True) -> Category | None:
        from urbanlens.dashboard.models.categories.model import Category

        category_name = category_name.lower()
        try:
            category, _created = Category.objects.get_or_create(name=category_name)
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
        return (
            f"Name: {self.effective_name}\n"
            f"Description: {self.description or ''}\n"
            f"Google Place Name: {self.place_name}\n"
            f"Priority: {self.priority}\n"
            f"Last Visited: {self.last_visited}\n"
            f"Status: {PinStatus(self.status).label}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.effective_name,
            "icon": self.icon,
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
            "status": PinStatus.get_name(self.status) or PinStatus.NOT_VISITED.label,
            "profile": self.profile.id,
            "rating": self.rating,
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
            Index(fields=["profile"]),
            Index(fields=["profile", "priority"]),
            Index(fields=["profile", "last_visited"]),
            Index(fields=["latitude", "longitude"]),
        ]
        unique_together = [
            ["latitude", "longitude", "profile"],
        ]
