"""Photon plugin: alternate OSM-backed reverse-geocoding panel and place names.

Photon (https://photon.komoot.io) is Komoot's free, keyless, open-source
geocoder over OpenStreetMap data - a redundant cross-check alongside the
existing Nominatim integration, using different indexing/ranking software
over the same underlying OSM dataset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider


class PhotonPanelSource(CoordinateGatedInfoPanelSource):
    """Photon's reverse-geocoded address for the pin's location."""

    key = "photon"
    cache_source = "photon"
    section_id = "photon-section"
    icon = "person_pin_circle"
    title = "Photon (OpenStreetMap)"

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates via Photon and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.photon import PhotonGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        place = PhotonGateway().reverse_geocode(lat, lng)
        LocationCache.set(pin.location, self.cache_source, place or {}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the address card from Photon's reverse-geocode result."""
        if not data or not data.get("name"):
            return None

        chips = [data["osm_value"].replace("_", " ").title()] if data.get("osm_value") else []
        street_parts = [data[key] for key in ("housenumber", "street") if data.get(key)]
        meta = [{"label": "Street", "value": " ".join(street_parts)}] if street_parts else []
        for key, label in (("locality", "Locality"), ("district", "District"), ("city", "City"), ("county", "County"), ("state", "State"), ("country", "Country"), ("postcode", "Postal Code")):
            if data.get(key):
                meta.append({"label": label, "value": data[key]})

        return {"heading_name": data.get("name"), "chips": chips, "meta": meta}


class PhotonPlugin(UrbanLensPlugin):
    """Photon geocoder: redundant OSM place names and an address panel."""

    name: ClassVar[str] = "photon"
    verbose_name: ClassVar[str] = "Photon"
    description: ClassVar[str] = (
        "Free, keyless, open-source OSM geocoder (Komoot) - shows an alternate reverse-geocoded address "
        "on the pin detail page and contributes place-name candidates."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for Photon's free public instance."""
        return {
            "photon": ServiceDefaults(
                display_name="Photon (Komoot geocoder)",
                calls_per_minute=10,
                calls_per_day=500,
                notes="Free public instance (photon.komoot.io), no API key. Be conservative - shared community resource.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Photon pin-detail panel."""
        return [PhotonPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute Photon's resolved name as a place-name candidate."""
        return [LocationCacheNameProvider(source="photon", cache_source="photon", keys=("name",), verbose_name="Photon")]
