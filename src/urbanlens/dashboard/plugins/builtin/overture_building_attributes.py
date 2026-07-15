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
    """Overture Maps building characteristics and nearby named places for the pin's location."""

    key = "overture_building_attributes"
    cache_source = "overture_building_attributes"
    section_id = "overture-building-section"
    icon = "apartment"
    title = "Building Characteristics"
    # Stays on the default (prefork) queue, not the fast thread-pool queue -
    # OvertureMapsGateway reads GeoParquet via pyarrow/geopandas (real
    # CPU-bound parsing/geometry work, same class of cost as BoundaryPanelSource's
    # shapely work), and several of those running concurrently on a thread
    # pool would cause enough GIL contention to slow down every other panel
    # sharing it. See PanelSource.queue.
    queue = "celery"

    def fetch(self, pin: Pin) -> None:
        """Look up the pinned building's Overture attributes and cache the result."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        gateway = OvertureMapsGateway()
        attributes = gateway.get_building_attributes(lat, lng) or {}
        nearby_places = gateway.get_nearby_places(lat, lng, radius_m=150, limit=5)
        LocationCache.set(pin.location, self.cache_source, {**attributes, "nearby_places": nearby_places}, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the building-characteristics card from Overture's attribute + nearby-places lookup."""
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

        facts = []
        for place in data.get("nearby_places") or []:
            category = (place.get("category") or "").replace("_", " ").title()
            status = place.get("operating_status")
            status_suffix = " (closed)" if status == "closed" else ""
            text = f"{place['name']}{status_suffix} - {category} ({place['distance_m']:.0f}m)" if category else f"{place['name']}{status_suffix} ({place['distance_m']:.0f}m)"
            facts.append({"icon": "storefront", "text": text})

        if not chips and not meta and not facts:
            return None

        return {"heading_name": data.get("primary_name"), "chips": chips, "facts": facts, "meta": meta}


class OvertureBuildingAttributesPlugin(UrbanLensPlugin):
    """Overture Maps building characteristics for pinned locations."""

    name: ClassVar[str] = "overture_building_attributes"
    verbose_name: ClassVar[str] = "Overture Building Characteristics"
    description: ClassVar[str] = (
        "Free, open-data building class/height/floor-count/roof details from Overture Maps' Buildings "
        "theme, plus nearby named places from its Places theme (same dataset already used for footprint "
        "boundaries) - no rate-limited HTTP endpoint of ours, since reads happen via S3 range requests, "
        "not a REST API."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the building-characteristics pin-detail panel."""
        return [OvertureBuildingAttributesPanelSource()]
