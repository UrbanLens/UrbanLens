"""Abstract mixin that adds structured address fields and derived properties to a model."""

from __future__ import annotations

from django.db.models import Model
from django.db.models.fields import CharField


class AddressableMixin(Model):
    """Adds Google Geocoding API address components and derived address properties.

    Only Location inherits this mixin - it holds the *canonical* address for a
    real-world place.  Pin does NOT inherit AddressableMixin; instead, Pin exposes
    the same properties as thin proxies that delegate to its location FK.

    Field names mirror the Google Geocoding API component types so that import
    code can copy response data directly without mapping.
    """

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
        return self.administrative_area_level_1

    @state.setter
    def state(self, value: str) -> None:
        self.administrative_area_level_1 = value

    @property
    def county(self) -> str | None:
        return self.administrative_area_level_2

    @county.setter
    def county(self, value: str) -> None:
        self.administrative_area_level_2 = value

    @property
    def city(self) -> str | None:
        return self.locality

    @city.setter
    def city(self, value: str) -> None:
        self.locality = value

    class Meta:
        abstract = True
