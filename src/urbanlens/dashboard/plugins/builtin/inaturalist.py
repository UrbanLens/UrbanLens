"""iNaturalist plugin: nearby wildlife/plant observations, sourced through REData."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class INaturalistPanelSource(CoordinateGatedInfoPanelSource):
    """Recent iNaturalist observations near the pin's location."""

    key = "inaturalist"
    cache_source = "inaturalist"
    section_id = "inaturalist-section"
    icon = "forest"
    title = "iNaturalist"
    # Shared by fetch() (the actual API search radius) and render_context()
    # (the footer link's radius param) so the "View nearby" link always
    # matches what was actually searched.
    radius_km: ClassVar[float] = 2

    def gate(self, pin: Pin) -> bool:
        """Also requires REData to be configured - this panel has no other data source."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Search REData's nature-observations registry for nearby sightings and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_nature_gateway import RedataNatureObservationsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        envelope = RedataNatureObservationsGateway().get_nearby_observations(lat, lng, radius_meters=self.radius_km * 1000, limit=10)
        LocationCache.set(pin.location, self.cache_source, {"observations": envelope.results}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the observation list from REData's nature-observations results."""
        observations = (data or {}).get("observations") or []
        if not observations:
            return None

        meta = []
        for obs in observations[:8]:
            label = obs.get("common_name") or obs.get("scientific_name") or "Unknown species"
            value = obs.get("observed_on") or "Date unknown"
            if (obs.get("attributes") or {}).get("obscured"):
                # A provider deliberately coarsened this sighting's location
                # (common for threatened species, sometimes by tens of
                # kilometres) - showing it as an ordinary precise sighting
                # would misrepresent it.
                value += " (approximate location)"
            meta.append(
                {
                    "label": label,
                    "value": value,
                    # Links straight to this specific sighting, not iNaturalist's homepage.
                    "href": obs.get("uri") or "",
                },
            )

        footer_url = "https://www.inaturalist.org/observations"
        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is not None and lng is not None:
            footer_url += f"?lat={lat}&lng={lng}&radius={self.radius_km}"

        return {
            "chips": [f"{len(observations)} nearby"],
            "meta": meta,
            "footer_link": {"url": footer_url, "label": "View nearby observations on iNaturalist"},
            "nested": True,
        }

    def debug_count(self, data: dict) -> int:
        """Number of nearby observations found."""
        return len((data or {}).get("observations") or [])


class INaturalistPlugin(UrbanLensPlugin):
    """iNaturalist nearby wildlife/plant observations for pinned locations, sourced through REData."""

    name: ClassVar[str] = "inaturalist"
    verbose_name: ClassVar[str] = "iNaturalist"
    description: ClassVar[str] = "Shows recent nearby wildlife/plant sightings on the pin detail page, sourced through REData's nature-observations registry (iNaturalist)."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the iNaturalist pin-detail panel."""
        return [INaturalistPanelSource()]
