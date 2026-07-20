"""iNaturalist plugin: nearby wildlife/plant observations panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


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

    def fetch(self, pin: Pin) -> None:
        """Search iNaturalist for nearby observations and cache the results."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.inaturalist import INaturalistGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        observations = INaturalistGateway().get_nearby_observations(lat, lng, radius_km=self.radius_km, limit=10)
        LocationCache.set(pin.location, self.cache_source, {"observations": observations}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the observation list from iNaturalist's nearby-observations search."""
        observations = (data or {}).get("observations") or []
        if not observations:
            return None

        meta = [
            {
                "label": obs.get("common_name") or obs.get("scientific_name") or "Unknown species",
                "value": obs.get("observed_on") or "Date unknown",
                # Links straight to this specific sighting, not iNaturalist's homepage.
                "href": obs.get("uri") or "",
            }
            for obs in observations[:8]
        ]

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
    """iNaturalist nearby wildlife/plant observations for pinned locations."""

    name: ClassVar[str] = "inaturalist"
    verbose_name: ClassVar[str] = "iNaturalist"
    description: ClassVar[str] = "Free, keyless, open-source community-science observations - shows recent nearby wildlife/plant sightings on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the iNaturalist API."""
        return {
            "inaturalist": ServiceDefaults(
                display_name="iNaturalist",
                calls_per_minute=30,
                calls_per_day=1000,
                notes="Free, keyless public API.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the iNaturalist pin-detail panel."""
        return [INaturalistPanelSource()]
