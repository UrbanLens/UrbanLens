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
from urbanlens.dashboard.services.external_data import LocationCachePanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.external_data import PanelSource


class OvertureBuildingAttributesPanelSource(LocationCachePanelSource):
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
