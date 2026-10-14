"""US Census TIGERweb plugin: US Census geography panel for pinned locations."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, PanelPlacement
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.plugins.base import PluginContext


class CensusTigerwebPanelSource(CoordinateGatedInfoPanelSource):
    """US Census state/county/place/tract geography for the pin's location."""

    key = "census_tigerweb"
    cache_source = "census_tigerweb"
    section_id = "census-tigerweb-section"
    icon = "flag"
    title = "US Census Geography"
    placement: ClassVar[PanelPlacement] = PanelPlacement.REGIONAL
    tab_label: ClassVar[str] = "US Census"
    tab_order: ClassVar[int] = 10

    def fetch(self, pin: Pin) -> None:
        """Look up the pin's coordinates in TIGERweb."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.census_tigerweb import CensusTigerwebGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        geography = CensusTigerwebGateway().get_geography(lat, lng)
        LocationCache.set(pin.location, self.cache_source, geography, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the geography card from TIGERweb lookup."""
        data = data or {}
        state = data.get("state")
        if not state:
            return None

        meta = [{"label": "State", "value": state["name"]}]
        for key, label in (
            ("county", "County"),
            ("place", "Place"),
            ("tract", "Census Tract"),
            ("zcta", "ZIP Code Area"),
            ("urban_area", "Urban Area"),
            ("cbsa", "Metro/Micro Area"),
        ):
            entry = data.get(key)
            if entry and entry.get("name"):
                meta.append({"label": label, "value": entry["name"]})

        facts = []
        tribal_land = data.get("tribal_land")
        if tribal_land and tribal_land.get("name"):
            facts.append({"icon": "warning", "text": f"Within {tribal_land['name']} - tribal land, access rules may differ"})

        return {"facts": facts, "meta": meta}


class CensusTigerwebPlugin(UrbanLensPlugin):
    """US Census Bureau TIGERweb geography lookups."""

    name: ClassVar[str] = "census_tigerweb"
    verbose_name: ClassVar[str] = "US Census TIGERweb"
    description: ClassVar[str] = "Free, keyless US Census Bureau geography (state/county/place/tract) for the pin's coordinates. USA only."
    author: ClassVar[str] = "UrbanLens"

    def setup(self, plugin: PluginContext) -> None:
        """TIGERweb rate-limit defaults and the Census geography pin-detail panel."""
        from urbanlens.dashboard.plugins.points import PANEL_SOURCES, SERVICE_DEFAULTS

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "census_tigerweb": ServiceDefaults(
                    display_name="US Census TIGERweb",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=30,
                    calls_per_day=2000,
                    usa_only=True,
                    notes="Free, keyless ArcGIS REST service.",
                ),
            },
        )

        plugin.extend(PANEL_SOURCES, [CensusTigerwebPanelSource()])
