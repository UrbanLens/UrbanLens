"""Photon plugin: alternate OSM-backed reverse-geocoding panel and place names."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class PhotonPanelSource(CoordinateGatedInfoPanelSource):
    """Photon's reverse-geocoded address for the pin's location."""

    key = "photon"
    cache_source = "photon"
    section_id = "photon-section"
    icon = "person_pin_circle"
    title = "Photon (OpenStreetMap)"

    def gate(self, pin: Pin) -> bool:
        """Only worth fetching when REData is configured - see the module docstring."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates via REData's Photon-tagged result and cache it."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_geocode_gateway import RedataGeocodeGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataGeocodeGateway().reverse_geocode(lat, lng, provider="photon")
        place = envelope.results[0] if envelope.results else {}
        LocationCache.set(pin.location, self.cache_source, place, query_key=f"{lat:.5f},{lng:.5f}")

    #: A successful lookup at a coordinate with no reverse-geocodable address
    #: caches a row that renders nothing, which showed as an empty tab.
    inspects_content = True

    def has_content(self, data: dict | None) -> bool:
        """Mirror of :meth:`render_context`'s own emptiness test.

        Returns:
            True when there is an address to show."""
        return bool(data and (data.get("locality") or data.get("region") or data.get("country")))

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the address card from REData's normalized address components."""
        if not data or not (data.get("locality") or data.get("region") or data.get("country")):
            return None

        heading_key = next(key for key in ("locality", "region", "country") if data.get(key))

        street_parts = [data[key] for key in ("house_number", "street") if data.get(key)]
        meta = [{"label": "Street", "value": " ".join(street_parts)}] if street_parts else []
        for key, label in (("locality", "Locality"), ("region", "Region"), ("country", "Country"), ("postal_code", "Postal Code")):
            if key != heading_key and data.get(key):
                meta.append({"label": label, "value": data[key]})

        return {"heading_name": data[heading_key], "chips": [], "meta": meta}


class PhotonPlugin(UrbanLensPlugin):
    """Photon geocoder: an alternate reverse-geocoded address panel, via REData.
    No longer calls Photon directly (see the module docstring) - REData's own ``redata_geocode`` service-rate-limit row (``rate_limiter.SERVICE_REGISTRY``) covers this, so there is no per-plugin rate limit to register here anymore."""

    name: ClassVar[str] = "photon"
    verbose_name: ClassVar[str] = "Photon"
    description: ClassVar[str] = "Alternate reverse-geocoded address on the Private Pin page, via REData's Photon-tagged geocode result."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Photon pin-detail panel."""
        return [PhotonPanelSource()]
