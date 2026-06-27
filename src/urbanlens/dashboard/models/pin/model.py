"""Pin model - a user's personal record for a location."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
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
from django.db.models.fields import BooleanField, CharField, DateField, DateTimeField, DecimalField, IntegerField, SlugField, TextField
from django.utils.text import slugify

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.pin.queryset import PinManager
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

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


class Pin(abstract.SecurityModel, abstract.AddressableModel):
    """A user's personal record for a physical location.

    Pin is the *personal* half of the two-model design:
    - Location  - one row per real-world place, shared across all users.
    - Pin       - one row per (user, place) pair; links to a Location via FK.

    A Pin belongs to exactly one Profile (user). Multiple users can each have
    their own Pin that references the same Location. Everything stored here is
    specific to that one user: their custom label, notes, visit history, status,
    priority, and an optional coordinate override to reposition the marker.
    """

    # Public-facing identifier. Non-sequential so users cannot enumerate other pins.
    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    # Optional per-user marker override. When unset, coordinates fall back to the
    # linked Location's canonical latitude/longitude.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # URL slug - unique per (profile, slug). Auto-generated from the effective name on first save.
    slug = SlugField(max_length=255, null=True, blank=True)

    # When True this pin is entirely personal: it will not be linked to a shared
    # Location and will never contribute to the community wiki.  User-specific
    # data (nickname, description, coordinates) must not be surfaced to others
    # regardless of this flag, but is_private=True is the explicit opt-out from
    # having any community presence at these coordinates.
    is_private = BooleanField(default=False)

    # User's custom label. None = show location.name instead (see effective_name).
    # Do NOT store canonical place names here - those belong on Location.
    nickname = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=255, null=True, blank=True)
    # User's personal notes. Unrelated to Location.description (place-level info).
    description = TextField(null=True, blank=True)
    priority = IntegerField(default=0)
    vulnerability = IntegerField(default=0)
    last_visited = DateTimeField(null=True, blank=True)
    custom_icon = ImageField(upload_to="pin_custom_icons/", null=True, blank=True)
    pin_type = CharField(choices=PinType.choices, default=PinType.LOCATION_MARKER, max_length=30)
    point = PointField(geography=True, default=Point(0, 0))
    
    # Direct hex color override for this pin (e.g. "#F44336"). Used by detail pins
    # when the user explicitly picks a color in the dialog.
    color = CharField(max_length=20, null=True, blank=True)

    # Detail-pin circle styling: background fill and border around the icon.
    # Opacity stored as 0-100 integer (percent).
    detail_bg_color = CharField(max_length=20, null=True, blank=True)
    detail_bg_opacity = IntegerField(default=80)
    detail_border_color = CharField(max_length=20, null=True, blank=True)
    detail_border_opacity = IntegerField(default=100)

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

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
    badges = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="pins",
    )
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
        parent_location_id: int | None
        parent_pin_id: int | None
        reviews: ReviewManager

    objects: PinManager = PinManager()  # pyright: ignore[reportIncompatibleVariableOverride]

    # ------------------------------------------------------------------
    # Effective values - resolve overrides against the linked Location
    # ------------------------------------------------------------------

    def display_badge(self) -> Badge | None:
        """Badge supplying the map icon, when the icon is inherited from a badge."""
        if self.custom_icon or self.icon:
            return None
        for badge in self.badges.exclude(kind="user").order_by("-order"):
            if badge.custom_icon and not badge.icon_is_overridden:
                return badge
            if badge.effective_icon:
                return badge
        return None

    @property
    def display_label(self) -> str:
        """Human-readable label: pin name when meaningful, otherwise street address."""
        if label := self.meaningful_name:
            return label
        if self.address:
            return self.address
        if self.location and self.location.address:
            return self.location.address
        return f"{self.effective_latitude}, {self.effective_longitude}"
    
    @property
    def effective_icon(self) -> str | None:
        """Icon to display for this pin following the priority chain."""
        if self.custom_icon:
            return self.custom_icon.url
        if self.icon:
            return self.icon
        if badge := self.display_badge():
            if badge.custom_icon and not badge.icon_is_overridden:
                return badge.custom_icon.url
            return badge.effective_icon
        return None

    @property
    def effective_color(self) -> str | None:
        """Color hex for the map icon circle, when one applies.

        Only an explicit ``pin.color`` or the badge that supplies the displayed icon
        may contribute. Other badges on the pin (e.g. a yellow tag when a green
        icon tag has no color) must not produce a circle.

        Prefetch badges (with customizations) when calling in bulk (e.g. get_map_data).
        """
        if self.color:
            return self.color
        if self.custom_icon or self.icon:
            return None
        winning = self.display_badge()
        if winning:
            return winning.effective_color
        return None

    @property
    def effective_name(self) -> str:
        """User's custom name, or the location's canonical name."""
        return self.nickname or (self.location.name if self.location else "")
    
    @property
    def meaningful_name(self) -> str | None:
        """The pin's name, or the location's canonical name if the pin has no name."""
        return self.effective_name if is_meaningful_name(self.effective_name) else None

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
        # TODO: Assess codebase, but this is probably deprecated since the addition of Badges more generically.
        
        from urbanlens.dashboard.models.badges.model import Badge

        category = Badge.objects.get(id=category_id, kind="category")
        self.badges.remove(*self.badges.filter(kind="category"))
        self.badges.add(category)
        self.save()

    def suggest_category(self, append_suggestion: bool = False) -> str | None:
        """Suggest a category using the pin's personal context and location metadata."""
        # TODO: This probably needs to be abstracted a bit due to the addition of generic badges (categories and tags). It should probably also live somewhere else.
        from urbanlens.dashboard.services.ai.factory import get_gateway
        from urbanlens.dashboard.services.ai.keywords import categorize_by_keywords

        keyword_parts = [
            p
            for p in (
                self.meaningful_name,
                self.place_name if self.has_place_name() else None,
            )
            if p
        ]
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
        if title := self.meaningful_name:
            prompt += f"location title: {title}\n"
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
                self.badges.add(category)
                if save:
                    self.save()
                return category
        except DatabaseError as e:
            logger.exception("failed to add category %s to pin -> %s", category_name, e)
        return None

    # ------------------------------------------------------------------
    # Serialisation / display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        status_labels = ", ".join(s.name for s in self.badges.filter(kind="status")) or "None"
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
            "slug": self.slug or str(self.uuid),
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
            "statuses": [{"id": s.id, "name": s.name, "color": s.color, "icon": s.icon} for s in self.badges.filter(kind="status")],
            "profile": self.profile.id,
            "rating": self.rating,
            "color": self.effective_color,
            "tags": [
                {"id": t.id, "name": t.name, "color": t.effective_color, "icon": t.effective_icon}
                for t in self.badges.filter(kind="tag")
            ],
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
            "color": self.effective_color,
            "bg_color": self.detail_bg_color or "",
            "bg_opacity": self.detail_bg_opacity,
            "border_color": self.detail_border_color or "",
            "border_opacity": self.detail_border_opacity,
        }

    def _generate_slug(self) -> str:
        """Derive a slug that is unique within this user's pins."""
        base = slugify(self.effective_name)[:200] or "pin"
        candidate = base
        n = 2
        qs = Pin.objects.filter(profile_id=self.profile_id)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        while qs.filter(slug=candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    class Meta(abstract.AddressableModel.Meta):
        db_table = "dashboard_user_pins"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"]),
            Index(fields=["profile"]),
            Index(fields=["profile", "priority"]),
            Index(fields=["profile", "last_visited"]),
            Index(fields=["profile", "updated"], name="dashboard_profile_update_idx"),
            Index(fields=["latitude", "longitude"]),
            Index(fields=["parent_pin"]),
            Index(fields=["parent_location"], name="dashboard_parent_loc_idx"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["latitude", "longitude", "profile"],
                condition=Q(parent_pin__isnull=True, parent_location__isnull=True),
                name="dashboard_pin_unique_location_per_profile",
            ),
            UniqueConstraint(
                fields=["profile", "slug"],
                condition=Q(slug__isnull=False),
                name="dashboard_pin_unique_slug_per_profile",
            ),
        ]
