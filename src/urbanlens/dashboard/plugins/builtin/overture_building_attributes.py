"""Overture Maps building-attributes plugin: physical building characteristics panel.

Free, open-data "real estate" context Overture actually publishes (it has no
year-built field, unlike what a quick read of some API-candidate lists might
suggest) - building class/subtype, height, floor count, and roof
construction, reusing the same ``OvertureMapsGateway`` already wired into the
boundary-provider chain (``services.apis.locations.boundaries.overture_maps``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import InfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class OvertureBuildingAttributesPanelSource(InfoPanelSource):
    """Overture Maps building characteristics for the pin's location."""

    key = "overture_building_attributes"
    cache_source = "overture_building_attributes"
    section_id = "overture-building-section"
    icon = "apartment"
    title = "Building Characteristics"

    def fetch(self, pin: Pin) -> None:
        """Look up the pinned building's Overture attributes and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        attributes = OvertureMapsGateway().get_building_attributes(lat, lng)
        LocationCache.set(pin.location, self.cache_source, attributes or {}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the building-characteristics card from Overture's attribute lookup."""
        if not data:
            return None

        chips = [data["subtype"].replace("_", " ").title()] if data.get("subtype") else []
        meta = []
        if data.get("height_m"):
            meta.append({"label": "Height", "value": f"{data['height_m']:.0f} m"})
        if data.get("num_floors"):
            meta.append({"label": "Floors", "value": str(data["num_floors"])})
        if data.get("roof_shape"):
            meta.append({"label": "Roof Shape", "value": data["roof_shape"].replace("_", " ").title()})
        if data.get("roof_material"):
            meta.append({"label": "Roof Material", "value": data["roof_material"].replace("_", " ").title()})

        if not chips and not meta:
            return None

        return {"heading_name": data.get("primary_name"), "chips": chips, "meta": meta}


class OvertureBuildingAttributesPlugin(UrbanLensPlugin):
    """Overture Maps building characteristics for pinned locations."""

    name: ClassVar[str] = "overture_building_attributes"
    verbose_name: ClassVar[str] = "Overture Building Characteristics"
    description: ClassVar[str] = (
        "Free, open-data building class/height/floor-count/roof details from Overture Maps' Buildings "
        "theme (the same dataset already used for footprint boundaries) - no rate-limited HTTP endpoint "
        "of ours, since reads happen via S3 range requests, not a REST API."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the building-characteristics pin-detail panel."""
        return [OvertureBuildingAttributesPanelSource()]
