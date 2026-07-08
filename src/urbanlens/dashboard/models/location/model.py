"""Location model - shared, immutable address/coordinate record for a place."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import SET_NULL, ForeignKey, Index
from django.db.models.fields import CharField, DecimalField, SlugField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.location.queryset import LocationManager

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.google_place.model import GooglePlace
    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.trips.model import TripActivity
    from urbanlens.dashboard.models.wiki.model import Wiki


logger = logging.getLogger(__name__)


class Location(abstract.DashboardModel):
    """Shared, immutable address/coordinate record for a physical place.

    Location is the *address* third of the place model:
    - Location  - one row per real-world address, shared and deduplicated by
      coordinates. Treated as immutable: when a pin's or wiki's coordinates
      change we find-or-create a *different* Location instead of mutating it.
    - Wiki      - one community page per Location (1:1); everything users edit
      collectively (name, description, security, badges, aliases, ...).
    - Pin       - one row per (user, place) pair; a user's personal record.

    A Location stores only what is derived from the address itself: coordinates,
    street components (via AddressableMixin), the linked GooglePlace, an
    external-source ``official_name``, and the cache of address-keyed external
    API results (``external_cache``).

    What does NOT belong here (all on Wiki now):
    - Community name / description -> Wiki.name / Wiki.description
    - Security indicators, badges, dates -> Wiki
    - Aliases, comments, edit history, photos -> Wiki
    A user's personal label/notes/visit history belong on Pin.
    """
    # External-source name for this place (e.g. from Google). User edits never
    # write this field; the community-editable name lives on Wiki.name.
    official_name = CharField(max_length=255, null=True, blank=True)

    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)

    street_number = CharField(max_length=50, null=True, blank=True)
    route = CharField(max_length=80, null=True, blank=True)
    locality = CharField(max_length=80, null=True, blank=True)
    administrative_area_level_1 = CharField(max_length=30, null=True, blank=True)
    administrative_area_level_2 = CharField(max_length=50, null=True, blank=True)
    administrative_area_level_3 = CharField(max_length=50, null=True, blank=True)
    country = CharField(max_length=20, default="United States")
    zipcode = CharField(max_length=10, null=True, blank=True)
    zipcode_suffix = CharField(max_length=10, null=True, blank=True)
    point = PointField(geography=True, default=Point(0, 0))
    
    google_place = ForeignKey(
        "dashboard.GooglePlace",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    if TYPE_CHECKING:
        google_place_id: int | None
        wiki: Wiki
        activities: DjangoManager[TripActivity]
        markup_items: DjangoManager[PinMarkup]

    objects = LocationManager()

    @property
    def address(self) -> str | None:
        """Full address string built from components."""
        parts = []
        if self.street_number:
            parts.append(self.street_number)
        if self.route:
            parts.append(f"{self.route},")
        if self.locality:
            parts.append(f"{self.locality},")
        if self.administrative_area_level_1:
            parts.append(self.administrative_area_level_1)
        if self.zipcode:
            parts.append(self.zipcode)
        return " ".join(parts) or None

    @property
    def address_basic(self) -> str | None:
        """Street number and route only."""
        parts = []
        if self.street_number:
            parts.append(self.street_number)
        if self.route:
            parts.append(self.route)
        return " ".join(parts) or None

    @property
    def address_extended(self) -> str | None:
        """Street address with city."""
        parts = []
        if self.street_number:
            parts.append(self.street_number)
        if self.route:
            parts.append(f"{self.route},")
        if self.locality:
            parts.append(self.locality)
        return " ".join(parts) or None

    @property
    def state(self) -> str | None:
        return self.administrative_area_level_1  # pyright: ignore[reportReturnType]

    @state.setter
    def state(self, value: str) -> None:
        self.administrative_area_level_1 = value

    @property
    def county(self) -> str | None:
        return self.administrative_area_level_2  # pyright: ignore[reportReturnType]

    @county.setter
    def county(self, value: str) -> None:
        self.administrative_area_level_2 = value

    @property
    def city(self) -> str | None:
        return self.locality  # pyright: ignore[reportReturnType]

    @city.setter
    def city(self, value: str) -> None:
        self.locality = value

    @property
    def cached_place_name(self) -> str | None:
        """Google place name from the linked cache row, if any."""
        stub = self.__dict__.get("_google_place_stub")
        if stub is not None and getattr(stub, "cached_place_name", None):
            return stub.cached_place_name
        if self.google_place_id and self.google_place is not None and self.google_place.cached_place_name:
            return self.google_place.cached_place_name
        return None

    @cached_place_name.setter
    def cached_place_name(self, value: str | None) -> None:
        """Assign a cached place name by creating or updating the shared GooglePlace row."""
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        if value is None and not self.google_place_id:
            return
        service = GooglePlaceService()
        google_place = service.get_or_create_for_coordinates(
            self.latitude,
            self.longitude,
            place_name=value,
            fetch_if_missing=value is None,
        )
        if self.pk:
            self.__class__.objects.filter(pk=self.pk).update(google_place_id=google_place.pk)
        self.google_place = google_place

    @property
    def cid(self) -> Decimal | None:
        """Google Maps CID from the linked cache row, if any."""
        if self.google_place_id and self.google_place is not None and self.google_place.cid is not None:
            return self.google_place.cid
        return None

    @cid.setter
    def cid(self, value: int | Decimal | None) -> None:
        """Store a Google Maps CID on the shared cache row for these coordinates."""
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        if value is None:
            return
        GooglePlaceService().set_cid_for_entity(self, value)

    @property
    def place_name(self) -> str | None:
        if self.cached_place_name:
            return self.cached_place_name
        return self.get_place_name()

    @property
    def display_name(self) -> str:
        """Best human-readable name: the community wiki name, else the official name.

        Reads the linked Wiki when present (prefetch with
        ``select_related("wiki")`` in bulk to avoid an extra query per row).
        
        # TODO: This should be assessed for deletion.
        """
        try:
            wiki = self.wiki
        except ObjectDoesNotExist:
            wiki = None
        if wiki is not None and wiki.name:
            return wiki.name
        return self.official_name or "Unnamed Location"
    
    def get_place_name(self) -> str | None:
        """Fetch the canonical place name from Google and cache it on GooglePlace."""
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        if self.latitude is None or self.longitude is None or not (-90 <= float(self.latitude) <= 90) or not (-180 <= float(self.longitude) <= 180):
            return "No Information Available"
        service = GooglePlaceService()
        google_place = service.get_or_create_for_coordinates(self.latitude, self.longitude)
        if self.pk and self.google_place_id != google_place.pk:
            self.__class__.objects.filter(pk=self.pk).update(google_place_id=google_place.pk)
            self.google_place_id = google_place.pk
            self.google_place = google_place
        return service.resolve_place_name(google_place)

    def has_place_name(self) -> bool:
        """True when the cached or resolved Google place name is useful for queries."""
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        return is_meaningful_name(self.place_name)

    def __str__(self):
        return self.official_name or f"Location({self.pk})"

    def to_json(self) -> dict:
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            "id": self.id,
            "official_name": self.official_name,
            "place_name": self.place_name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    def save(self, *args, **kwargs) -> None:
        """Auto-generate a routing slug and sync the PostGIS point before saving."""
        if self.latitude is not None and self.longitude is not None:
            lon = float(self.longitude)
            lat = float(self.latitude)
            self.point = Point(lon, lat, srid=4326)
        
        super().save(*args, **kwargs)

    def __setattr__(self, name: str, value) -> None:
        """Support lightweight GooglePlace doubles on unsaved model instances.

        Django's foreign-key descriptor only accepts real ``GooglePlace`` model
        instances. A few unit tests exercise the place-name helpers on prepared,
        unsaved models with a small duck-typed object that exposes
        ``cached_place_name`` and ``pk``. Preserve that fast path without
        weakening saved model relations.
        """
        if name == "google_place" and value is not None:
            from urbanlens.dashboard.models.google_place.model import GooglePlace

            if not isinstance(value, GooglePlace) and hasattr(value, "cached_place_name"):
                self.__dict__["_google_place_stub"] = value
                self.__dict__["google_place_id"] = getattr(value, "pk", None)
                return
        super().__setattr__(name, value)
        
    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_loc_uuid"),
            Index(fields=["latitude", "longitude"], name="idxdb_loc_lat_long"),
            Index(fields=["official_name"], name="idxdb_loc_offname"),
            Index(fields=["google_place"], name="idxdb_loc_gplace"),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
