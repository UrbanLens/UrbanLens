"""Site conditions plugin: land cover, walkability and soil for a pin, via REData.

Three single-answer USA-only domains (NLCD land cover, EPA walkability, USDA
soil survey) folded into one panel rather than three thin ones - each
contributes a fact or two, and any source that fails to answer is simply
absent rather than blanking the panel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)


class SiteConditionsPanelSource(CoordinateGatedInfoPanelSource):
    """Land cover, walkability and soil composition at the pin's location."""

    key = "redata_site_conditions"
    cache_source = "redata_site_conditions"
    section_id = "site-conditions-section"
    icon = "landscape"
    title = "Site Conditions"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self, pin: Pin) -> bool:
        """Requires US coordinates (all three sources are USA-only) and REData to be configured."""
        return super().gate(pin) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Fetch all three domains, caching whichever answered.

        Each domain is fetched independently: one source's outage must not
        blank the facts the others can still supply, so failures are logged
        and recorded as an absent key rather than raised.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_land_cover_gateway import RedataLandCoverGateway
        from urbanlens.dashboard.services.apis.locations.redata_soil_gateway import RedataSoilGateway
        from urbanlens.dashboard.services.apis.locations.redata_walkability_gateway import RedataWalkabilityGateway

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)

        data: dict[str, Any] = {}
        failed = 0
        for domain, fetch_one in (
            ("land_cover", lambda: RedataLandCoverGateway().get_land_cover(lat, lng).results),
            ("walkability", lambda: RedataWalkabilityGateway().get_walkability(lat, lng).results),
            ("soil", lambda: RedataSoilGateway().get_soil_components(lat, lng).results),
        ):
            try:
                data[domain] = fetch_one()
            except LocationContextUnavailableError as exc:
                failed += 1
                logger.warning("Site-conditions %s lookup failed: %s", domain, exc)
        if failed and not data:
            # Every domain failed, so there is nothing to cache but the outage.
            # The existence of the row marks this source as fetched, so writing
            # an empty dict would leave the panel permanently blank rather than
            # retried - the same shape as the SearXNG image cache. A *partial*
            # result is still written: the domains that answered are real data,
            # and the missing ones re-fetch when the row next goes stale.
            logger.warning("Site-conditions: every domain failed, leaving it unfetched to retry")
            return
        LocationCache.set(pin.location, self.cache_source, data, query_key=f"{lat:.5f},{lng:.5f}")

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """One facts row for the point answers, a meta grid for the soil composition."""
        data = data or {}
        land_cover = next(iter(data.get("land_cover") or []), None)
        walkability = next(iter(data.get("walkability") or []), None)
        soil = data.get("soil") or []
        if not (land_cover or walkability or soil):
            return None

        facts = []
        if land_cover and land_cover.get("class_name"):
            facts.append({"icon": "forest", "text": land_cover["class_name"]})
        if walkability and walkability.get("index") is not None:
            band = walkability.get("band") or ""
            facts.append({"icon": "directions_walk", "text": f"Walkability {walkability['index']:.0f}/20" + (f" ({band})" if band else "")})
        if walkability:
            transit = walkability.get("transit_distance_meters")
            # Null is the majority answer nationally - a real fact, not a gap.
            facts.append({"icon": "directions_bus", "text": f"Transit stop {transit:,.0f} m" if transit is not None else "No transit stop nearby"})

        meta = []
        if soil:
            map_unit = soil[0].get("map_unit_name") or ""
            if map_unit:
                meta.append({"label": "Soil map unit", "value": map_unit})
            # Composition, dominant first - deliberately no "overall" figure,
            # because the minority component is often the one that matters.
            for component in soil[:3]:
                name = component.get("component_name") or "Unnamed component"
                percent = component.get("component_percent")
                details = [detail for detail in (component.get("drainage_class"), component.get("hydrologic_group") and f"group {component['hydrologic_group']}") if detail]
                label = f"{name} ({percent:.0f}%)" if isinstance(percent, (int, float)) else name
                meta.append({"label": label, "value": ", ".join(details) or "Unrated"})

        return {"facts": facts, "meta": meta}

    def debug_count(self, data: dict) -> int:
        """Number of domains that answered."""
        return sum(1 for key in ("land_cover", "walkability", "soil") if (data or {}).get(key))


class SiteConditionsPlugin(UrbanLensPlugin):
    """Land-cover, walkability and soil context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_site_conditions"
    verbose_name: ClassVar[str] = "Site Conditions"
    description: ClassVar[str] = "Shows NLCD land cover, the EPA walkability index and USDA soil composition for the pin's site on the detail page, sourced through REData. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the site-conditions pin-detail panel."""
        return [SiteConditionsPanelSource()]
