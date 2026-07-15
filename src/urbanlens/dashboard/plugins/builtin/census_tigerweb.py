"""US Census TIGERweb plugin: US Census geography panel for pinned locations."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class CensusTigerwebPanelSource(CoordinateGatedInfoPanelSource):
    """US Census state/county/place/tract geography for the pin's location."""

    key = "census_tigerweb"
    cache_source = "census_tigerweb"
    section_id = "census-tigerweb-section"
    icon = "flag"
    title = "US Census Geography"

    def fetch(self, pin: Pin) -> None:
        """Look up the pin's coordinates in TIGERweb and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.census_tigerweb import CensusTigerwebGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        geography = CensusTigerwebGateway().get_geography(lat, lng)
        LocationCache.set(pin.location, self.cache_source, geography, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the geography card from TIGERweb's state/county/place/tract lookup."""
        data = data or {}
        state = data.get("state")
        if not state:
            return None

        meta = [{"label": "State", "value": state["name"]}]
        for key, label in (("county", "County"), ("place", "Place"), ("tract", "Census Tract")):
            entry = data.get(key)
            if entry and entry.get("name"):
                meta.append({"label": label, "value": entry["name"]})

        return {"meta": meta}


class CensusTigerwebPlugin(UrbanLensPlugin):
    """US Census Bureau TIGERweb geography lookups for pinned locations. USA only."""

    name: ClassVar[str] = "census_tigerweb"
    verbose_name: ClassVar[str] = "US Census TIGERweb"
    description: ClassVar[str] = (
        "Free, keyless US Census Bureau geography (state/county/place/tract) for the pin's coordinates. "
        "USA only."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the TIGERweb ArcGIS REST service."""
        return {
            "census_tigerweb": ServiceDefaults(
                display_name="US Census TIGERweb",
                calls_per_minute=30,
                calls_per_day=2000,
                usa_only=True,
                notes="Free, keyless ArcGIS REST service.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Census geography pin-detail panel."""
        return [CensusTigerwebPanelSource()]
