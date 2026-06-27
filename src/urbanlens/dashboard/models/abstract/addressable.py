"""Abstract mixin that adds structured address fields and derived properties to a model."""

from __future__ import annotations

import logging

from django.contrib.gis.db.models import PointField, PolygonField
from django.contrib.gis.geos import Point, Polygon
from django.db.models.fields import CharField, DecimalField

from urbanlens.dashboard.models.abstract import Model
from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

# ~50 m radius expressed in degrees (at mid-latitudes). Used as the default
# bounding box when a new Location is created without an explicit boundary.
_DEFAULT_BBOX_DEGREES = 0.00045


class AddressableModel(Model):
    """Adds Google Geocoding API address components and derived address properties.

    Only Location inherits this mixin - it holds the *canonical* address for a
    real-world place.  Pin does NOT inherit AddressableMixin; instead, Pin exposes
    the same properties as thin proxies that delegate to its location FK.

    Field names mirror the Google Geocoding API component types so that import
    code can copy response data directly without mapping.
    """
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
    cached_place_name = CharField(max_length=255, null=True, blank=True)
    point = PointField(geography=True, default=Point(0, 0))

    # Google Maps CID - unsigned 64-bit identifier embedded in place URLs.
    # Stored as Decimal to handle values above signed int64 range (> 2^63-1).
    # Used to de-duplicate Location rows on import and to look up Places API data.
    cid = DecimalField(max_digits=20, decimal_places=0, null=True, blank=True, unique=True)

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
            # TODO: Catch specific exception type
            logger.debug("Google place-name lookup failed for location %s: %s", self.pk, exc)
            result = None
        if not result:
            result = "No Information Available"
        if not self.cached_place_name:
            self.cached_place_name = result
            if self.pk:
                # Use update() to persist without triggering post_save signals
                self.__class__.objects.filter(pk=self.pk).update(cached_place_name=result)
        return result

    def has_place_name(self) -> bool:
        name = self.place_name
        return bool(name) and name != "No Information Available"
    
    # Names produced by Google Maps when a place has no real identity. A pin
    # whose effective_name is one of these has no useful search query to build.
    # TODO: This doesn't belong here and should be defined in a more elegant way.
    _MEANINGLESS_NAMES: frozenset[str] = frozenset({"Dropped pin", "No Information Available", ""})

    class Meta(Model.Meta):
        abstract = True
